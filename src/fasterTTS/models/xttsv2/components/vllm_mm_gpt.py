import functools
import math
import random
import uuid
from array import array

import numpy as np
import torch
import torch.nn as nn
from typing import List, Optional, Union, Iterable, Tuple, Mapping, Dict

from torch import Tensor
from transformers import PretrainedConfig, GPT2Config
from vllm.attention import AttentionMetadata
from vllm.config import CacheConfig, MultiModalConfig
from vllm.distributed import get_pp_group
from vllm.inputs import InputContext, INPUT_REGISTRY, DecoderOnlyInputs, token_inputs
from vllm.model_executor.layers.logits_processor import LogitsProcessor
from vllm.model_executor.layers.quantization import QuantizationConfig
from vllm.model_executor.layers.sampler import Sampler, SamplerOutput
from vllm.model_executor.layers.vocab_parallel_embedding import VocabParallelEmbedding, ParallelLMHead
from vllm.model_executor.model_loader.weight_utils import default_weight_loader
from vllm.model_executor.models.gpt2 import GPT2Block
from vllm.model_executor.models.utils import make_layers, make_empty_intermediate_tensors_factory
from vllm.model_executor.sampling_metadata import SamplingMetadata
from vllm.multimodal import MULTIMODAL_REGISTRY, MultiModalInputs
from vllm.sequence import IntermediateTensors, SequenceData, VLLM_TOKEN_ID_ARRAY_TYPE
from vllm.model_executor.models.interfaces import SupportsMultiModal, SupportsPP


class LearnedPositionEmbeddings(nn.Module):
    def __init__(self, seq_len, model_dim, init=0.02, relative=False, supports_pp=False):
        super().__init__()
        # nn.Embedding
        self.emb = VocabParallelEmbedding(seq_len, model_dim) if supports_pp else nn.Embedding(seq_len, model_dim)
        # Initializing this way is standard for GPT-2
        self.emb.weight.data.normal_(mean=0.0, std=init)
        self.relative = relative
        self.seq_len = seq_len

    def forward(self, x):
        sl = x.shape[1]
        if self.relative:
            start = random.randint(sl, self.seq_len) - sl
            return self.emb(torch.arange(start, start + sl, device=x.device))
        else:
            return self.emb(torch.arange(0, sl, device=x.device))

    def get_fixed_embedding(self, ind: torch.Tensor, dev: torch.device) -> torch.Tensor:
        """Get position embeddings with batch support.

        Handles both single and batched inputs, returning embeddings that can be
        directly added to input embeddings of the same shape.

        Args:
            ind: Position indices tensor. Can be single or batched
                 Shape: [..., seq_len] or [seq_len]
            dev: Target device for the embeddings

        Returns:
            Position embeddings tensor matching input shape plus embedding dimension
            Shape: [batch_size, seq_len, model_dim] or [1, 1, model_dim]

        Example:
            >>> pos_emb = LearnedPositionEmbeddings(100, 64)
            >>> # Batched input
            >>> batch_indices = torch.zeros((3, 5))  # batch_size=3, seq_len=5
            >>> embeddings = pos_emb.get_fixed_embedding(batch_indices, 'cuda')
            >>> embeddings.shape  # Returns: [3, 5, 64]
        """
        if ind.shape[0] > 1:
            pos_embeddings = []
            for index in ind:
                # Create embeddings for each position in the sequence
                pos_embeddings.append(self.emb(index))

            # Shape: [1, seq_len, model_dim] -> [batch_size, seq_len, model_dim]
            return torch.stack(pos_embeddings, dim=0)
        else:
            # Handle single input
            # Shape: [1, 1, model_dim]
            return self.emb(torch.tensor([ind], device=dev)).unsqueeze(0)


def get_xtts_max_audio_tokens(ctx: InputContext) -> int:
    """Calculate maximum audio tokens based on text context and audio duration."""
    return 32 # the conditoning perciever output


def dummy_seq_data_for_xtts(
        ctx: InputContext,
        seq_len: int,
        audio_count: int,
) -> SequenceData:
    """Create dummy sequence data for XTTS profiling."""
    # Calculate audio token space needed
    audio_placeholder = array(
        VLLM_TOKEN_ID_ARRAY_TYPE,
        [1]
    ) * 32 # the conditioning perceiver output

    # Add separator between chunks
    audio_token_ids = (audio_placeholder + array(VLLM_TOKEN_ID_ARRAY_TYPE, [1])) * audio_count

    # Fill remaining sequence with padding
    other_token_ids = array(VLLM_TOKEN_ID_ARRAY_TYPE, [1]) * (seq_len - len(audio_token_ids))
    # not -1 since we add the start audio token

    return SequenceData(
        audio_token_ids +
        other_token_ids
    )

def dummy_conditioning_for_xtts(
        ctx: InputContext,
        seq_len: int,
        audio_count: int,
) -> dict:
    """Create dummy conditioning data for XTTS."""
    return {
        "audio": {
            "embeds":[
            torch.zeros(
                (seq_len, ctx.model_config.hf_config.hidden_size),
                dtype=ctx.model_config.dtype) for _ in range(audio_count)
        ],
            "is_logits_only_mode": False,
        }
    }


def dummy_data_for_xtts(
        ctx: InputContext,
        seq_len: int,
        mm_counts: Mapping[str, int],
) -> Tuple[SequenceData, dict]:
    """Create complete dummy data for XTTS profiling."""
    audio_count = mm_counts["audio"]
    seq_data = dummy_seq_data_for_xtts(ctx, seq_len, audio_count)
    cond_data = dummy_conditioning_for_xtts(ctx, seq_len, audio_count)
    return seq_data, cond_data


def input_mapper_for_xtts(ctx: InputContext, data: Union[Dict, List[Tensor]]) -> MultiModalInputs:
    """Map input data to XTTS format."""

    assert isinstance(data, dict), "XTTS MultiModal input data must be a dictionary with keys: 'embeds', 'is_logits_only_mode'"

    embeds = data.get("embeds")
    is_logits_only_mode = data.get("is_logits_only_mode", False)

    # Each item should be a torch tensor
    for audio_input in embeds:
        if not isinstance(audio_input, Tensor):
            raise NotImplementedError(f"Unsupported data type: {type(audio_input)}")

    return MultiModalInputs({"cond_latents": embeds,
                             "is_logits_only_mode": is_logits_only_mode,
                             })


def input_processor_for_xtts2_gpt(ctx: InputContext, inputs: DecoderOnlyInputs):
    """
    We'll accomodate for the extra contditioning token and for the start audio token,
    we actually insert a -1 repeated for the differecne in length between the conditioning and the tokenized text
    and then we add 1 for the start audio token
    Args:
        ctx:
        inputs:

    Returns:

    """
    multi_modal_data = inputs.get("multi_modal_data")
    audio_dict = multi_modal_data['audio']
    audio = audio_dict.get('embeds')

    is_last_decoding_pass = audio_dict.get("is_logits_only_mode", False)

    prompt_token_ids = inputs.get("prompt_token_ids")

    if not is_last_decoding_pass:
        # we fill everything with 0 since we don't actually needs text token ids, it would mess up in the sampling step
        new_token_ids = ([1] * (audio.shape[0])) + [ctx.model_config.hf_config.start_audio_token] # add the start audio generation token
    else:
        new_token_ids = ([1] * audio.shape[0]) + prompt_token_ids
    # the encoding had already been done externally to reuse the embeddings for later use but we
    # account for the new token that will be added before generation
    new_prompt = None
    return token_inputs(prompt_token_ids=new_token_ids,
                 prompt=new_prompt,
                 multi_modal_data=multi_modal_data)

from vllm.model_executor.models.ultravox import UltravoxModel

@MULTIMODAL_REGISTRY.register_input_mapper("audio", input_mapper_for_xtts)
@MULTIMODAL_REGISTRY.register_max_multimodal_tokens("audio", get_xtts_max_audio_tokens)
@INPUT_REGISTRY.register_dummy_data(dummy_data_for_xtts)
@INPUT_REGISTRY.register_input_processor(input_processor_for_xtts2_gpt)
class XttsGPT(nn.Module, SupportsMultiModal, SupportsPP):
    def __init__(
            self,
            config: PretrainedConfig,
            multimodal_config: MultiModalConfig,
            cache_config: Optional[CacheConfig] = None,
            quant_config: Optional[QuantizationConfig] = None,
    ):
        super().__init__()
        self.config = config
        self.quant_config = quant_config

        # Core GPT components
        self.gpt = GPT2Model(
            config,
            cache_config,
            quant_config,
            prefix="gpt"
        )
        self.final_norm =  nn.LayerNorm(config.hidden_size, bias=True, eps=config.layer_norm_epsilon)
        # Output head for mel tokens
        self.mel_head = ParallelLMHead(
            config.num_audio_tokens,
            config.hidden_size,
            bias=True,
            quant_config=quant_config,
            prefix="mel_head"
        )
        self.audio_start_generation_token = config.start_audio_token

        # Initialize logits processor and sampler
        logit_scale = getattr(config, "logit_scale", 1.0)
        self.logits_processor = LogitsProcessor(config.num_audio_tokens,
                                                config.num_audio_tokens,
                                                logit_scale)
        self.sampler = Sampler()

    @staticmethod
    def check_is_logits_only_mode(is_logits_only_mode):

        # First check if it's a boolean
        if isinstance(is_logits_only_mode, bool):
            return is_logits_only_mode

        # Then check if it's a tensor
        if torch.is_tensor(is_logits_only_mode):
            # if it's a scalar tensor, return the value
            if is_logits_only_mode.numel() == 1:
                return bool(is_logits_only_mode.item())
            # for non-scalar tensors, check if all elements are the same
            return is_logits_only_mode.any()

        # Fallback
        return bool(is_logits_only_mode)

    def _calculate_start_token_indices(self, cond_latents: List[torch.Tensor]) -> List[int]:
        """Calcola gli indici dove inserire i token di start.

        Args:
            cond_latents: Lista di tensori di condizionamento

        Returns:
            Lista di indici dove inserire i token di start
        """
        indices = []
        current_idx = 0

        for cond_latent in cond_latents:
            # Aggiungi la lunghezza del segmento corrente
            current_idx += cond_latent.shape[0]
            # Aggiungi l'indice per il token di start dopo questo segmento
            indices.append(current_idx)
            # Incrementa per il token di start che verrà aggiunto
            current_idx += 1

        return indices

    # noinspection PyMethodOverriding
    def forward(
            self,
            input_ids: torch.Tensor,
            positions: torch.Tensor,
            kv_caches: List[torch.Tensor],
            attn_metadata: AttentionMetadata,
            intermediate_tensors: Optional["IntermediateTensors"] = None,
            cond_latents: Optional[torch.Tensor] = None,
            is_logits_only_mode: bool = False,
            **kwargs,
    ) -> Union[torch.Tensor, "IntermediateTensors"]:
        """Forward pass following VLLM pattern."""
        # it is not the first iter either if the cond latents are emtpy or if the kv_caches are not empty
        is_first_iteration = len(input_ids) > 1 and torch.isin(input_ids, torch.tensor([1, 1024], device=input_ids.device)).all()

        #assert len(input_ids) == 1 or (cond_latents is not None and not is_first_iteration), "Conditioning data (voice conditioning+text_embeddings) is required for XTTS"

        is_logits_only_mode = self.check_is_logits_only_mode(is_logits_only_mode)

        hidden_states = self.gpt(
            input_ids=input_ids,
            position_ids=positions,
            kv_caches=kv_caches,
            attn_metadata=attn_metadata,
            intermediate_tensors=intermediate_tensors,
            # this is the conditioning input ( voice conditioning + text_embeds )
            input_embeds=cond_latents,
            is_first_iteration=is_first_iteration,
            is_logits_only_mode=is_logits_only_mode
        )

        return hidden_states

    def compute_logits(
            self,
            hidden_states: torch.Tensor,
            sampling_metadata: SamplingMetadata,
    ) -> Optional[torch.Tensor]:

        # normalize the hidden states
        hidden_states = self.final_norm(hidden_states)

        # Check if we need to collect hidden states
        sampling_params = sampling_metadata.seq_groups[0].sampling_params
        if hasattr(sampling_params, 'hidden_state_collector'):
            # Call the collector directly with the hidden states
            sampling_params.hidden_state_collector(hidden_states, None)  # The request_id is already bound

        # Compute logits using the mel_head
        logits = self.logits_processor(self.mel_head, hidden_states, sampling_metadata)
        return logits

    def sample(
            self,
            logits: torch.Tensor,
            sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        next_tokens = self.sampler(logits, sampling_metadata)
        return next_tokens

    def load_weights(self, weights: Iterable[Tuple[str, torch.Tensor]]):
        """Load weights following VLLM pattern."""
        params_dict = dict(self.named_parameters(remove_duplicate=False))
        loaded_names = set()
        for name, loaded_weight in weights:
            if name not in params_dict:
                #print(f"Skipping loading of {name} bc it is not found") # used to check if all weights were loaded
                continue

            param = params_dict[name]
            if "c_attn" in name or "c_proj" in name or "c_fc" in name:
                if name.endswith(".weight"):
                    loaded_weight = loaded_weight.t()

            weight_loader = getattr(param, "weight_loader", default_weight_loader)
            weight_loader(param, loaded_weight)
            loaded_names.add(name)
        # used to check if all weights were loaded
        assert set(params_dict.keys()) - loaded_names == set(), \
            (f"Missing weights: {set(params_dict.keys()) - loaded_names}, "
             f"this probably means you are using an incompatible model ")

class GPT2Model(nn.Module):

    def __init__(
            self,
            config: GPT2Config,
            cache_config: Optional[CacheConfig] = None,
            quant_config: Optional[QuantizationConfig] = None,
            prefix: str = "",
    ):
        super().__init__()
        self.config = config
        assert not config.add_cross_attention
        assert not config.scale_attn_by_inverse_layer_idx
        assert not config.reorder_and_upcast_attn
        self.embed_dim = config.hidden_size
        self.wte = VocabParallelEmbedding(config.num_audio_tokens, self.embed_dim)
        self.wpe = (
            LearnedPositionEmbeddings(config.max_audio_tokens + 3, config.decoder_input_dim)
            if config.max_audio_tokens != -1
            else functools.partial(config.null_position_embeddings, dim=config.decoder_input_dim)
        )
        self.start_layer, self.end_layer, self.h = make_layers(
            config.num_hidden_layers,
            lambda prefix: GPT2Block(
                config, cache_config, quant_config, prefix=prefix),
            prefix=f"{prefix}.h")
        self.ln_f = nn.LayerNorm(self.embed_dim, eps=config.layer_norm_epsilon)
        self.make_empty_intermediate_tensors = (
            make_empty_intermediate_tensors_factory(["hidden_states"],
                                                    config.hidden_size))


    def forward(
            self,
            input_ids: torch.Tensor,
            position_ids: torch.Tensor,
            kv_caches: List[torch.Tensor],
            attn_metadata: AttentionMetadata,
            intermediate_tensors: Optional[IntermediateTensors],
            # we pass this so that we can concatenate the text and conditioning input
            input_embeds: Optional[torch.Tensor] = None,
            is_first_iteration: bool = False,
            is_logits_only_mode: bool = False,
    ) -> Union[torch.Tensor, IntermediateTensors]:

        if get_pp_group().is_first_rank:
            # if we are not doing the final conversion from token to latent and it is first pass(prefill)
            if is_first_iteration and not is_logits_only_mode:
                input_ids = input_ids[-1].reshape(1, 1)
            elif is_logits_only_mode:
                # we remove the contidioning input and keep just the audio token
                if isinstance(input_embeds, list):
                    starting_idx = []
                    for input_embed in input_embeds:
                        starting_idx.append(input_embed.shape[0])
                    ending_ids = attn_metadata.seq_lens  # list

                    # First sequence: from starting_idx[0] to ending_ids[0]
                    cumulative_starts = [starting_idx[0]]  # First starts at its own index
                    cumulative_ends = [ending_ids[0]]  # First ends at its ending_id

                    # For subsequent sequences:
                    # Start = previous_end + current_start
                    # End = previous_end + current_end
                    for i in range(1, len(starting_idx)):
                        next_start = cumulative_ends[i - 1] + starting_idx[i]
                        next_end = cumulative_ends[i - 1] + ending_ids[i]
                        cumulative_starts.append(next_start)
                        cumulative_ends.append(next_end)

                    ids_for_unpacking = [end-start for start, end in zip(cumulative_starts, cumulative_ends)]

                    input_ids = torch.cat([
                        input_ids[start:end].reshape(1, -1)
                        for start, end in zip(cumulative_starts, cumulative_ends)
                    ], dim=-1)
                    position_ids = torch.cat([
                        position_ids[start:end].reshape(1, -1)
                        for start, end in zip(cumulative_starts, cumulative_ends)
                    ], dim= -1).squeeze(0)
                else:
                    input_ids = input_ids[input_embeds.shape[1]:].reshape(1, -1)
                    position_ids = position_ids[input_embeds.shape[1]:]#.reshape(1, -1)
            else:
                input_ids = input_ids

            audio_inputs_embeds = self.wte(input_ids).squeeze(0)

            # weird but they to it like this in the xtts2 model
            position_embeds = self.wpe.get_fixed_embedding(
                    position_ids, input_ids.device
            ) if not is_first_iteration \
                    else self.wpe(audio_inputs_embeds.reshape(-1, 1)) # we need to reshape to 2D tensor or useless?

            hidden_states = audio_inputs_embeds + position_embeds

            if isinstance(input_embeds, list) and is_logits_only_mode:
                hidden_states = list(hidden_states.split(ids_for_unpacking, dim=0)) # whby this tho?

            if is_first_iteration or is_logits_only_mode:
                # We concat the text and audio conditioning input in the sequence dimension
                if isinstance(input_embeds, list):
                    input_embeds = [input_embed.view(-1, input_embed.shape[-1]) for input_embed in input_embeds]
                else:
                    input_embeds = input_embeds.view(-1, input_embeds.shape[-1]) # we ensure we have a 2D tensor

                if not isinstance(input_embeds, list) and input_embeds.shape[0] == attn_metadata.num_prefill_tokens:
                    # this is during profiling, wee need to remove the last token
                    # the attn_metadata.num_prefill_tokens(prompt len) should be == to input_embeds.shape[0] - 1
                    # to account for the start audio gen embedding that will be cat to the text embeddings
                    input_embeds = input_embeds[:-1]

            if is_first_iteration or is_logits_only_mode:
                # we concatenate the conditioning input to the text conditioning input
                if isinstance(input_embeds, list):
                        hidden_states = torch.cat([
                                tensor for pair in zip(input_embeds, [hidden_states] * len(input_embeds)
                                                    if not isinstance(hidden_states, list) else hidden_states)
                                for tensor in pair
                            ], dim=0)
                else:
                    hidden_states = torch.cat([input_embeds, hidden_states], dim=0)

            #flatten the hidden state
            hidden_states = hidden_states.view(-1, self.embed_dim)
        else:
            assert intermediate_tensors is not None
            hidden_states = intermediate_tensors["hidden_states"]

        for i in range(self.start_layer, self.end_layer):
            layer = self.h[i]
            hidden_states = layer(hidden_states,
                                  kv_caches[i - self.start_layer],
                                  attn_metadata)

        if not get_pp_group().is_last_rank:
            return IntermediateTensors({"hidden_states": hidden_states})

        hidden_states = self.ln_f(hidden_states)
        return hidden_states
