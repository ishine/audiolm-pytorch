import math
from functools import partial
from typing import Optional, Union

import torch
from torch import nn, einsum
from torch.autograd import grad as torch_grad
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence

from einops import rearrange, repeat

from audiolm_pytorch.vq_wav2vec import FairseqVQWav2Vec
from audiolm_pytorch.hubert_kmeans import HubertWithKmeans

from audiolm_pytorch.t5 import t5_encode_text, get_encoded_dim, DEFAULT_T5_NAME

from torchaudio.functional import resample

from audiolm_pytorch.soundstream import SoundStream

# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def ceil_div(numer, denom):
    return (numer + denom - 1) // denom

def remainder_needed_until_multiple(n, mult):
    return (ceil_div(n, mult) * mult) - n

def round_down_nearest_multiple(val, mult):
    return (val // mult) * mult

# attention related utils

def grad_shrink(t, alpha = 0.1):
    return t * alpha + t.detach() * (1 - alpha)

# classifier free guidance functions

def prob_mask_like(shape, prob, device):
    if prob == 1:
        return torch.ones(shape, device = device, dtype = torch.bool)
    elif prob == 0:
        return torch.zeros(shape, device = device, dtype = torch.bool)
    else:
        return torch.zeros(shape, device = device).float().uniform_(0, 1) < prob

# removing unique consecutives in the semantic token ids
# important detail noted by @eonglints

def append_eos_id(ids, eos_id):
    b, device = ids.shape[0], ids.device
    eos_ids = torch.ones(1, device = device).long() * eos_id
    eos_ids = repeat(eos_ids, '1 -> b 1', b = b)
    ids = torch.cat((ids, eos_ids), dim = -1)
    return ids

def batch_unique_consecutive(t, pad_value = 0.):
    unique_arr = [torch.unique_consecutive(el) for el in t.unbind(dim = 0)]
    return pad_sequence(unique_arr, batch_first = True, padding_value = pad_value)

# relative positional bias

class RelativePositionBias(nn.Module):
    def __init__(
        self,
        num_buckets = 32,
        max_distance = 128,
        heads = 8
    ):
        super().__init__()
        self.num_buckets = num_buckets
        self.max_distance = max_distance
        self.relative_attention_bias = nn.Embedding(num_buckets, heads)

    @staticmethod
    def _relative_position_bucket(relative_position, causal = True, num_buckets = 32, max_distance = 128):
        ret = 0

        n = -relative_position
        n = torch.max(n, torch.zeros_like(n))

        max_exact = num_buckets // 2
        is_small = n < max_exact

        val_if_large = max_exact + (
            torch.log(n.float() / max_exact) / math.log(max_distance / max_exact) * (num_buckets - max_exact)
        ).long()

        val_if_large = torch.min(val_if_large, torch.full_like(val_if_large, num_buckets - 1))

        ret += torch.where(is_small, n, val_if_large)
        return ret

    def forward(self, i, j, device):

        q_pos = torch.arange(j - i, j, dtype = torch.long, device = device)
        k_pos = torch.arange(j, dtype = torch.long, device = device)

        rel_pos = k_pos[None, :] - q_pos[:, None]

        rp_bucket = self._relative_position_bucket(rel_pos, num_buckets = self.num_buckets, max_distance = self.max_distance)
        values = self.relative_attention_bias(rp_bucket)

        return rearrange(values, 'i j h -> h i j')

# feedforward

class GEGLU(nn.Module):
    def forward(self, x):
        x, gate = x.chunk(2, dim = -1)
        return F.gelu(gate) * x

def FeedForward(dim, mult = 4):
    inner_dim = int(dim * 2 * mult / 3)
    return nn.Sequential(
        nn.LayerNorm(dim),
        nn.Linear(dim, inner_dim * 2, bias = False),
        GEGLU(),
        nn.Linear(inner_dim, dim, bias = False)
    )

# attention

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        causal = False,
        dim_head = 64,
        dim_context = None,
        heads = 8,
        norm_context = False,
        num_null_kv = 0
    ):
        super().__init__()
        self.heads = heads
        self.scale = dim_head ** -0.5
        self.causal = causal
        inner_dim = dim_head * heads

        dim_context = default(dim_context, dim)

        self.norm = nn.LayerNorm(dim)
        self.context_norm = nn.LayerNorm(dim_context) if norm_context else nn.Identity()

        self.num_null_kv = num_null_kv
        self.null_kv = nn.Parameter(torch.randn(2, num_null_kv, dim_head))

        self.to_q = nn.Linear(dim, inner_dim, bias = False)
        self.to_kv = nn.Linear(dim_context, dim_head * 2, bias = False)
        self.to_out = nn.Linear(inner_dim, dim, bias = False)

    def forward(
        self,
        x,
        context = None,
        mask = None,
        attn_bias = None
    ):
        b = x.shape[0]

        if exists(context):
            context = self.context_norm(context)

        kv_input = default(context, x)

        x = self.norm(x)

        q, k, v = self.to_q(x), *self.to_kv(kv_input).chunk(2, dim = -1)

        if self.num_null_kv > 0:
            null_k, null_v = repeat(self.null_kv, 'kv n d -> kv b n d', b = b).unbind(dim = 0)
            k = torch.cat((null_k, k), dim = -2)
            v = torch.cat((null_v, v), dim = -2)

        q = rearrange(q, 'b n (h d) -> b h n d', h = self.heads)

        q = q * self.scale

        sim = einsum('b h i d, b j d -> b h i j', q, k)

        if exists(attn_bias):
            attn_bias = F.pad(attn_bias, (self.num_null_kv, 0), value = 0.)
            sim = sim + attn_bias

        if exists(mask):
            mask = F.pad(mask, (self.num_null_kv, 0), value = True)
            mask = rearrange(mask, 'b j -> b 1 1 j')
            sim = sim.masked_fill(~mask, -torch.finfo(sim.dtype).max)

        if self.causal:
            i, j = sim.shape[-2:]
            causal_mask = torch.ones((i, j), dtype = torch.bool, device = x.device).triu(j - i + 1)
            sim = sim.masked_fill(causal_mask, -torch.finfo(sim.dtype).max)

        attn = sim.softmax(dim = -1)

        out = einsum('b h i j, b j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# transformer

class Transformer(nn.Module):
    def __init__(
        self,
        *,
        dim,
        depth,
        dim_context = None,
        cross_attend = False,
        grad_shrink_alpha = 0.1,
        **kwargs
    ):
        super().__init__()
        self.grad_shrink = partial(grad_shrink, alpha = grad_shrink_alpha)

        self.layers = nn.ModuleList([])

        self.rel_pos_bias = RelativePositionBias()

        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                Attention(dim = dim, causal = True, **kwargs),
                Attention(dim = dim, dim_context = dim_context, num_null_kv = 1, norm_context = True, **kwargs) if cross_attend else None,
                FeedForward(dim = dim)
            ]))

        self.norm = nn.LayerNorm(dim)

    def forward(
        self,
        x,
        self_attn_mask = None,
        context = None,
        context_mask = None
    ):
        n, device = x.shape[1], x.device

        x = self.grad_shrink(x) # from cogview paper, adopted by GLM 130B LLM, decreases likelihood of attention net instability

        rel_pos_bias = self.rel_pos_bias(n, n, device = device)

        for attn, cross_attn, ff in self.layers:
            x = attn(x, attn_bias = rel_pos_bias, mask = self_attn_mask) + x

            if exists(cross_attn):
                assert exists(context)

                x = cross_attn(x, context = context, mask = context_mask)

            x = ff(x) + x

        return self.norm(x)

# the three hierarchical transformers

class SemanticTransformer(nn.Module):
    def __init__(
        self,
        *,
        dim,
        num_semantic_tokens = None,
        t5_name = DEFAULT_T5_NAME,
        has_condition = False,
        cond_drop_prob = 0.5,
        wav2vec: Optional[Union[FairseqVQWav2Vec, HubertWithKmeans]] = None,
        unique_consecutive = True,
        grad_shrink_alpha = 0.1,
        pad_id = -1,
        **kwargs
    ):
        super().__init__()
        assert exists(wav2vec) or exists(num_semantic_tokens)

        if exists(wav2vec):
            num_semantic_tokens = default(num_semantic_tokens, wav2vec.codebook_size)
            assert num_semantic_tokens == wav2vec.codebook_size

        self.has_condition = has_condition
        self.embed_text = partial(t5_encode_text, name = t5_name)
        self.cond_drop_prob = cond_drop_prob

        self.unique_consecutive = unique_consecutive

        self.start_token = nn.Parameter(torch.randn(dim))

        self.semantic_embedding = nn.Embedding(num_semantic_tokens + 1, dim)
        self.eos_id = num_semantic_tokens
        self.pad_id = pad_id

        self.wav2vec = wav2vec
        self.transformer = Transformer(dim = dim, dim_context = get_encoded_dim(t5_name), cross_attend = has_condition, grad_shrink_alpha = grad_shrink_alpha, **kwargs)
        self.to_logits = nn.Linear(dim, num_semantic_tokens + 1)

    def forward(
        self,
        *,
        raw_wave = None,
        ids = None,
        return_loss = False,
        text = None,
        text_embeds = None,
        cond_drop_prob = None
    ):
        device = next(self.parameters()).device

        assert exists(raw_wave) ^ exists(ids)

        if not exists(ids):
            assert exists(self.wav2vec)
            ids = self.wav2vec(raw_wave, flatten = False)

        b = ids.shape[0]

        ids = append_eos_id(ids, self.eos_id)

        if self.unique_consecutive:
            ids = batch_unique_consecutive(ids, pad_value = self.pad_id)

        has_text = exists(text) or exists(text_embeds)
        assert not (self.has_condition ^ has_text)

        text_mask = None
        if not exists(text_embeds) and exists(text):
            with torch.no_grad():
                text_embeds = self.embed_text(text, output_device = device)
                text_mask = torch.any(text_embeds != 0, dim = -1)

        cond_drop_prob = default(cond_drop_prob, self.cond_drop_prob)

        if exists(text_mask) and cond_drop_prob > 0:
            keep_mask = prob_mask_like((b,), 1 - cond_drop_prob, device = device)
            text_mask = rearrange(keep_mask, 'b -> b 1') & text_mask

        if return_loss:
            labels, ids = ids.clone(), ids[:, :-1]

        tokens = self.semantic_embedding(ids)

        start_tokens = repeat(self.start_token, 'd -> b 1 d', b = ids.shape[0])

        tokens = torch.cat((start_tokens, tokens), dim = 1)

        tokens = self.transformer(tokens, context = text_embeds, context_mask = text_mask)
        logits = self.to_logits(tokens)

        if not return_loss:
            return logits

        loss = F.cross_entropy(
            rearrange(logits, 'b n c -> b c n'),
            labels,
            ignore_index = self.pad_id
        )

        return loss

class CoarseTransformer(nn.Module):
    def __init__(
        self,
        *,
        codebook_size,
        num_coarse_quantizers,
        dim,
        num_semantic_tokens = None,
        t5_name = DEFAULT_T5_NAME,
        has_condition = False,
        cond_drop_prob = 0.5,
        grad_shrink_alpha = 0.1,
        wav2vec: Optional[Union[FairseqVQWav2Vec, HubertWithKmeans]] = None,
        **kwargs
    ):
        super().__init__()
        assert exists(wav2vec) or exists(num_semantic_tokens)

        if exists(wav2vec):
            num_semantic_tokens = default(num_semantic_tokens, wav2vec.codebook_size)
            assert num_semantic_tokens == wav2vec.codebook_size

        self.has_condition = has_condition
        self.embed_text = partial(t5_encode_text, name = t5_name)
        self.cond_drop_prob = cond_drop_prob

        self.start_token = nn.Parameter(torch.randn(dim))

        self.semantic_eos_id = num_semantic_tokens
        self.semantic_embedding = nn.Embedding(num_semantic_tokens + 1, dim)

        self.coarse_eos_id = codebook_size
        codebook_size_with_eos = codebook_size + 1
        self.coarse_embedding = nn.Embedding(num_coarse_quantizers * codebook_size_with_eos, dim)

        self.wav2vec = wav2vec
        self.transformer = Transformer(dim = dim, dim_context = get_encoded_dim(t5_name), cross_attend = has_condition, grad_shrink_alpha = grad_shrink_alpha, **kwargs)

        self.codebook_size = codebook_size
        self.num_coarse_quantizers = num_coarse_quantizers

        self.to_semantic_logits = nn.Linear(dim, num_semantic_tokens + 1)
        self.coarse_logit_weights = nn.Parameter(torch.randn(num_coarse_quantizers, codebook_size_with_eos, dim))

    def forward(
        self,
        *,
        semantic_token_ids,
        coarse_token_ids,
        self_attn_mask = None,
        text = None,
        text_embeds = None,
        cond_drop_prob = None
    ):
        b, device = semantic_token_ids.shape[0], semantic_token_ids.device

        has_text = exists(text) or exists(text_embeds)
        assert not (self.has_condition ^ has_text)

        text_mask = None
        if not exists(text_embeds) and exists(text):
            with torch.no_grad():
                text_embeds = self.embed_text(text, output_device = device)
                text_mask = torch.any(text_embeds != 0, dim = -1)

        cond_drop_prob = default(cond_drop_prob, self.cond_drop_prob)

        if exists(text_mask) and cond_drop_prob > 0:
            keep_mask = prob_mask_like((b,), 1 - cond_drop_prob, device = device)
            text_mask = rearrange(keep_mask, 'b -> b 1') & text_mask

        coarse_token_ids, semantic_token_ids = map(lambda t: rearrange(t, 'b ... -> b (...)'), (coarse_token_ids, semantic_token_ids))

        offsets = self.codebook_size * torch.arange(self.num_coarse_quantizers, device = device)
        offsets = repeat(offsets, 'q -> 1 (n q)', n = ceil_div(coarse_token_ids.shape[-1], self.num_coarse_quantizers))
        offsets = offsets[:, :coarse_token_ids.shape[-1]]
        coarse_token_ids = coarse_token_ids + offsets

        semantic_tokens = self.semantic_embedding(semantic_token_ids)
        coarse_tokens = self.coarse_embedding(coarse_token_ids)

        semantic_seq_len = semantic_tokens.shape[1]

        start_tokens = repeat(self.start_token, 'd -> b 1 d', b = b)

        tokens = torch.cat((start_tokens, semantic_tokens, coarse_tokens), dim = 1)

        tokens = self.transformer(tokens, context = text_embeds, self_attn_mask = self_attn_mask, context_mask = text_mask)

        pred_semantic_tokens, pred_coarse_tokens = tokens[:, :semantic_seq_len], tokens[:, semantic_seq_len:]

        # semantic logits

        semantic_logits = self.to_semantic_logits(pred_semantic_tokens)

        # get coarse logits

        n = pred_coarse_tokens.shape[1]
        nq = round_down_nearest_multiple(n, self.num_coarse_quantizers)

        pred_coarse_tokens_groupable, pred_coarse_tokens_remainder = pred_coarse_tokens[:, :nq], pred_coarse_tokens[:, nq:]

        pred_coarse_tokens_groupable = rearrange(pred_coarse_tokens_groupable, 'b (n q) d -> b n q d', q = self.num_coarse_quantizers)

        coarse_logits_groupable = einsum('q c d, b n q d -> b n q c', self.coarse_logit_weights, pred_coarse_tokens_groupable)

        coarse_logits_groupable = rearrange(coarse_logits_groupable, 'b n q c -> b (n q) c')

        remainder_num_quantizers = pred_coarse_tokens_remainder.shape[1]

        if remainder_num_quantizers > 0:
            coarse_logits_remainder = einsum('q c d, b q d -> b q c', self.coarse_logit_weights[:remainder_num_quantizers], pred_coarse_tokens_remainder)

            coarse_logits = torch.cat((coarse_logits_groupable, coarse_logits_remainder), dim = 1)
        else:
            coarse_logits = coarse_logits_groupable

        return semantic_logits, coarse_logits

class FineTransformer(nn.Module):
    def __init__(
        self,
        *,
        num_coarse_quantizers,
        num_fine_quantizers,
        codebook_size,
        dim,
        t5_name = DEFAULT_T5_NAME,
        has_condition = False,
        cond_drop_prob = 0.5,
        grad_shrink_alpha = 0.1,
        **kwargs
    ):
        super().__init__()
        self.has_condition = has_condition
        self.embed_text = partial(t5_encode_text, name = t5_name)
        self.cond_drop_prob = cond_drop_prob

        self.start_token = nn.Parameter(torch.randn(dim))

        codebook_size_with_eos = codebook_size + 1

        self.coarse_embedding = nn.Embedding(num_coarse_quantizers * codebook_size_with_eos, dim)
        self.fine_embedding = nn.Embedding(num_fine_quantizers * codebook_size_with_eos, dim)

        self.eos_id = codebook_size

        self.transformer = Transformer(dim = dim, dim_context = get_encoded_dim(t5_name), cross_attend = has_condition, grad_shrink_alpha = grad_shrink_alpha, **kwargs)

        self.codebook_size = codebook_size
        self.num_coarse_quantizers = num_coarse_quantizers
        self.num_fine_quantizers = num_fine_quantizers

        self.coarse_logit_weights = nn.Parameter(torch.randn(num_coarse_quantizers, codebook_size_with_eos, dim))
        self.fine_logit_weights = nn.Parameter(torch.randn(num_fine_quantizers, codebook_size_with_eos, dim))

    def forward(
        self,
        coarse_token_ids,
        fine_token_ids,
        text = None,
        text_embeds = None,
        cond_drop_prob = None
    ):
        b, device = coarse_token_ids.shape[0], coarse_token_ids.device
        has_text = exists(text) or exists(text_embeds)
        assert not (self.has_condition ^ has_text)

        text_mask = None
        if not exists(text_embeds) and exists(text):
            with torch.no_grad():
                text_embeds = self.embed_text(text, output_device = device)
                text_mask = torch.any(text_embeds != 0, dim = -1)

        cond_drop_prob = default(cond_drop_prob, self.cond_drop_prob)

        if exists(text_mask) and cond_drop_prob > 0:
            keep_mask = prob_mask_like((b,), 1 - cond_drop_prob, device = device)
            text_mask = rearrange(keep_mask, 'b -> b 1') & text_mask

        coarse_token_ids, fine_token_ids = map(lambda t: rearrange(t, 'b ... -> b (...)'), (coarse_token_ids, fine_token_ids))

        b, n = coarse_token_ids.shape

        coarse_offsets = self.codebook_size * torch.arange(self.num_coarse_quantizers, device = device)
        coarse_offsets = repeat(coarse_offsets, 'q -> 1 (n q)', n = ceil_div(coarse_token_ids.shape[-1], self.num_coarse_quantizers))
        coarse_offsets = coarse_offsets[:, :coarse_token_ids.shape[-1]]
        coarse_token_ids = coarse_token_ids + coarse_offsets

        fine_offsets = self.codebook_size * torch.arange(self.num_fine_quantizers, device = device)
        fine_offsets = repeat(fine_offsets, 'q -> 1 (n q)', n = ceil_div(fine_token_ids.shape[-1], self.num_fine_quantizers))
        fine_offsets = fine_offsets[:, :fine_token_ids.shape[-1]]
        fine_token_ids = fine_token_ids + fine_offsets

        coarse_tokens = self.coarse_embedding(coarse_token_ids)
        fine_tokens = self.fine_embedding(fine_token_ids)

        start_tokens = repeat(self.start_token, 'd -> b 1 d', b = b)

        tokens = torch.cat((start_tokens, coarse_tokens, fine_tokens), dim = 1)

        tokens = self.transformer(tokens, context = text_embeds, context_mask = text_mask)

        pred_coarse_tokens, pred_fine_tokens = tokens[:, :n], tokens[:, n:]

        # get coarse logits

        pred_coarse_seq_len = pred_coarse_tokens.shape[1]

        padding = remainder_needed_until_multiple(pred_coarse_seq_len, self.num_coarse_quantizers)

        if padding != 0:
            pred_coarse_tokens = F.pad(pred_coarse_tokens, (0, 0, 0, padding), value = 0.)

        pred_coarse_tokens = rearrange(pred_coarse_tokens, 'b (n q) d -> b n q d', q = self.num_coarse_quantizers)

        coarse_logits = einsum('q c d, b n q d -> b n q c', self.coarse_logit_weights, pred_coarse_tokens)

        coarse_logits = rearrange(coarse_logits, 'b n q c -> b (n q) c')

        coarse_logits = coarse_logits[:, :pred_coarse_seq_len]

        # get fine logits

        pred_fine_seq_len = pred_fine_tokens.shape[1]
        nq = round_down_nearest_multiple(pred_fine_seq_len, self.num_fine_quantizers)

        pred_fine_tokens_groupable, pred_fine_tokens_remainder = pred_fine_tokens[:, :nq], pred_fine_tokens[:, nq:]

        pred_fine_tokens_groupable = rearrange(pred_fine_tokens_groupable, 'b (n q) d -> b n q d', q = self.num_fine_quantizers)

        fine_logits_groupable = einsum('q c d, b n q d -> b n q c', self.fine_logit_weights, pred_fine_tokens_groupable)

        fine_logits_groupable = rearrange(fine_logits_groupable, 'b n q c -> b (n q) c')

        remainder_num_quantizers = pred_fine_tokens_remainder.shape[1]

        if remainder_num_quantizers > 0:
            fine_logits_remainder = einsum('q c d, b q d -> b q c', self.fine_logit_weights[:remainder_num_quantizers], pred_fine_tokens_remainder)

            fine_logits = torch.cat((fine_logits_groupable, fine_logits_remainder), dim = 1)
        else:
            fine_logits = fine_logits_groupable

        return coarse_logits, fine_logits

# training wrappers

class FineTransformerWrapper(nn.Module):
    def __init__(
        self,
        *,
        transformer: FineTransformer,
        soundstream: Optional[SoundStream] = None,
        num_coarse_quantize = 3
    ):
        super().__init__()
        self.soundstream = soundstream
        self.transformer = transformer

        assert num_coarse_quantize > 0
        self.num_coarse_quantize = num_coarse_quantize

    def forward(
        self,
        *,
        raw_wave = None,
        coarse_token_ids = None,
        fine_token_ids = None,
        return_loss = False,
        **kwargs
    ):
        assert exists(raw_wave) ^ (exists(coarse_token_ids) and exists(fine_token_ids)), 'either raw waveform (raw_wav) is given, or coarse and fine token ids (coarse_token_ids, fine_token_ids)'

        if exists(raw_wave):
            assert exists(self.soundstream), 'SoundStream must be provided if given raw wave for training'

            with torch.no_grad():
                self.soundstream.eval()
                _, indices, _ = self.soundstream(raw_wave, return_encoded = True)
                coarse_token_ids, fine_token_ids = indices[..., :self.num_coarse_quantize], indices[..., self.num_coarse_quantize:]

        coarse_token_ids = rearrange(coarse_token_ids, 'b ... -> b (...)')
        fine_token_ids = rearrange(fine_token_ids, 'b ... -> b (...)')

        coarse_token_ids = append_eos_id(coarse_token_ids, self.transformer.eos_id)
        fine_token_ids = append_eos_id(fine_token_ids, self.transformer.eos_id)

        if return_loss:
            coarse_labels, fine_labels = coarse_token_ids, fine_token_ids.clone()
            fine_token_ids = fine_token_ids[:, :-1]

        coarse_logits, fine_logits = self.transformer(
            coarse_token_ids = coarse_token_ids,
            fine_token_ids = fine_token_ids,
            **kwargs
        )

        if not return_loss:
            return coarse_logits, fine_logits

        coarse_logits, fine_logits = map(lambda t: rearrange(t, 'b n c -> b c n'), (coarse_logits, fine_logits))

        num_coarse_logits, num_fine_logits = coarse_logits.shape[-1], fine_logits.shape[-1]

        coarse_loss = F.cross_entropy(
            coarse_logits,
            coarse_labels
        )

        fine_loss = F.cross_entropy(
            fine_logits,
            fine_labels
        )

        return (coarse_loss * num_coarse_logits + fine_loss * num_fine_logits) / (num_coarse_logits + num_fine_logits)

class CoarseTransformerWrapper(nn.Module):
    def __init__(
        self,
        *,
        transformer: FineTransformer,
        soundstream: Optional[SoundStream]  = None,
        wav2vec: Optional[Union[FairseqVQWav2Vec, HubertWithKmeans]] = None,
        num_coarse_quantize = 3,
        pad_id = -1,
        unique_consecutive = True
    ):
        super().__init__()
        self.soundstream = soundstream
        self.wav2vec = wav2vec

        self.transformer = transformer
        self.unique_consecutive = unique_consecutive
        self.pad_id = pad_id

        assert num_coarse_quantize > 0
        self.num_coarse_quantize = num_coarse_quantize

    def forward(
        self,
        *,
        semantic_token_ids = None,
        raw_wave = None,
        coarse_token_ids = None,
        return_loss = False,
        **kwargs
    ):
        assert exists(raw_wave) or exists(semantic_token_ids), 'either raw waveform (raw_wave) is given or semantic token ids are given (semantic_token_ids)'
        assert exists(raw_wave) or exists(coarse_token_ids), 'either raw waveform (raw_wav) is given, or coarse and fine token ids (coarse_token_ids, fine_token_ids)'
        assert not all(map(exists, (raw_wave, semantic_token_ids, coarse_token_ids)))

        if not exists(semantic_token_ids):
            assert exists(self.wav2vec), 'VQWav2Vec must be be provided if given raw wave for training'
            semantic_token_ids = self.wav2vec(raw_wave, flatten = False)

        if not exists(coarse_token_ids):
            assert exists(self.soundstream), 'SoundStream must be provided if given raw wave for training'

            with torch.no_grad():
                self.soundstream.eval()
                _, indices, _ = self.soundstream(raw_wave, return_encoded = True)
                coarse_token_ids, _ = indices[..., :self.num_coarse_quantize], indices[..., self.num_coarse_quantize:]

        coarse_token_ids = rearrange(coarse_token_ids, 'b ... -> b (...)')
        semantic_token_ids = rearrange(semantic_token_ids, 'b ... -> b (...)')

        coarse_token_ids = append_eos_id(coarse_token_ids, self.transformer.coarse_eos_id)
        semantic_token_ids = append_eos_id(semantic_token_ids, self.transformer.semantic_eos_id)

        if self.unique_consecutive:
            semantic_token_ids = batch_unique_consecutive(semantic_token_ids, pad_value = self.pad_id)

        if return_loss:
            semantic_labels, coarse_labels = semantic_token_ids, coarse_token_ids.clone()
            coarse_token_ids = coarse_token_ids[:, :-1]

        self_attn_mask = None
        if self.unique_consecutive:
            self_attn_mask = semantic_token_ids != -1
            semantic_token_ids = semantic_token_ids.masked_fill(~self_attn_mask, 0)
            self_attn_mask = F.pad(self_attn_mask, (1, coarse_token_ids.shape[-1]), value = True)

        semantic_logits, coarse_logits = self.transformer(
            semantic_token_ids = semantic_token_ids,
            coarse_token_ids = coarse_token_ids,
            self_attn_mask = self_attn_mask,
            **kwargs
        )

        if not return_loss:
            return semantic_logits, coarse_logits

        coarse_logits, semantic_logits = map(lambda t: rearrange(t, 'b n c -> b c n'), (coarse_logits, semantic_logits))

        if self.unique_consecutive:
            num_coarse_logits, num_semantic_logits = coarse_labels.numel(), (semantic_labels != self.pad_id).sum()
        else:
            num_coarse_logits, num_semantic_logits = coarse_logits.shape[-1], semantic_logits.shape[-1]

        semantic_loss = F.cross_entropy(
            semantic_logits,
            semantic_labels,
            ignore_index = self.pad_id
        )

        coarse_loss = F.cross_entropy(
            coarse_logits,
            coarse_labels
        )

        return (semantic_loss * num_semantic_logits + coarse_loss * num_coarse_logits) / (num_semantic_logits + num_coarse_logits)

# audio LM

class AudioLM(nn.Module):
    def __init__(
        self,
        *,
        soundstream: SoundStream,
        semantic_transformer: SemanticTransformer,
        coarse_transformer: CoarseTransformer,
        fine_transformer: FineTransformer,
    ):
        super().__init__()
        self.soundstream = soundstream
        self.semantic = semantic_transformer
        self.coarse = coarse_transformer
        self.fine = fine_transformer

    def forward(self, x):
        raise NotImplemented
