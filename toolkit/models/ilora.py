import math
import weakref

import torch
import torch.nn as nn
from typing import TYPE_CHECKING, List, Dict, Any
from toolkit.models.clip_fusion import ZipperBlock
from toolkit.models.zipper_resampler import ZipperModule, ZipperResampler
import sys
from toolkit.paths import REPOS_ROOT
sys.path.append(REPOS_ROOT)
from ipadapter.ip_adapter.resampler import  Resampler
from collections import OrderedDict

if TYPE_CHECKING:
    from toolkit.lora_special import LoRAModule
    from toolkit.stable_diffusion_model import StableDiffusion


class MLP(nn.Module):
    def __init__(self, in_dim, out_dim, hidden_dim, dropout=0.1, use_residual=True):
        super().__init__()
        if use_residual:
            assert in_dim == out_dim
        self.layernorm = nn.LayerNorm(in_dim)
        self.fc1 = nn.Linear(in_dim, hidden_dim)
        self.fc2 = nn.Linear(hidden_dim, out_dim)
        self.dropout = nn.Dropout(dropout)
        self.use_residual = use_residual
        self.act_fn = nn.GELU()

    def forward(self, x):
        residual = x
        x = self.layernorm(x)
        x = self.fc1(x)
        x = self.act_fn(x)
        x = self.fc2(x)
        x = self.dropout(x)
        if self.use_residual:
            x = x + residual
        return x

class LoRAGenerator(torch.nn.Module):
    def __init__(
            self,
            input_size: int = 768,  # projection dimension
            hidden_size: int = 768,
            head_size: int = 512,
            num_mlp_layers: int = 1,
            output_size: int = 768,
            dropout: float = 0.5
    ):
        super().__init__()
        self.input_size = input_size

        self.output_size = output_size
        self.lin_in = nn.Linear(input_size, hidden_size)

        self.mlp_blocks = nn.Sequential(*[
            MLP(hidden_size, hidden_size, hidden_size, dropout=dropout, use_residual=True) for _ in range(num_mlp_layers)
        ])
        self.head = nn.Linear(hidden_size, head_size, bias=False)
        self.norm = nn.LayerNorm(head_size)

        self.flatten = nn.Flatten()
        self.output = nn.Linear(head_size, self.output_size)
        # for each output block. multiply weights by 0.01
        with torch.no_grad():
            self.output.weight.data *= 0.01

    # allow get device
    @property
    def device(self):
        return next(self.parameters()).device

    @property
    def dtype(self):
        return next(self.parameters()).dtype

    def forward(self, embedding):
        if len(embedding.shape) == 2:
            embedding = embedding.unsqueeze(1)

        x = self.lin_in(embedding)
        x = self.mlp_blocks(x)
        x = self.head(x)
        x = self.norm(x)

        head_output = x

        x = self.output(head_output)
        return x.squeeze(1)


class InstantLoRAMidModule(torch.nn.Module):
    def __init__(
            self,
            index: int,
            lora_module: 'LoRAModule',
            instant_lora_module: 'InstantLoRAModule',
            up_shape: list = None,
            down_shape: list = None,
    ):
        super(InstantLoRAMidModule, self).__init__()
        self.up_shape = up_shape
        self.down_shape = down_shape
        self.index = index
        self.lora_module_ref = weakref.ref(lora_module)
        self.instant_lora_module_ref = weakref.ref(instant_lora_module)

        self.embed = None

    def down_forward(self, x, *args, **kwargs):
        # get the embed
        self.embed = self.instant_lora_module_ref().img_embeds[self.index]
        down_size = math.prod(self.down_shape)
        down_weight = self.embed[:, :down_size]

        batch_size = x.shape[0]

        # unconditional
        if down_weight.shape[0] * 2 == batch_size:
            down_weight = torch.cat([down_weight] * 2, dim=0)

        weight_chunks = torch.chunk(down_weight, batch_size, dim=0)
        x_chunks = torch.chunk(x, batch_size, dim=0)

        x_out = []
        for i in range(batch_size):
            weight_chunk = weight_chunks[i]
            x_chunk = x_chunks[i]
            # reshape
            weight_chunk = weight_chunk.view(self.down_shape)
            # run a simple lenear layer with the down weight
            x_chunk = x_chunk @ weight_chunk.T
            x_out.append(x_chunk)
        x = torch.cat(x_out, dim=0)
        return x


    def up_forward(self, x, *args, **kwargs):
        self.embed = self.instant_lora_module_ref().img_embeds[self.index]
        up_size = math.prod(self.up_shape)
        up_weight = self.embed[:, -up_size:]

        batch_size = x.shape[0]

        # unconditional
        if up_weight.shape[0] * 2 == batch_size:
            up_weight = torch.cat([up_weight] * 2, dim=0)

        weight_chunks = torch.chunk(up_weight, batch_size, dim=0)
        x_chunks = torch.chunk(x, batch_size, dim=0)

        x_out = []
        for i in range(batch_size):
            weight_chunk = weight_chunks[i]
            x_chunk = x_chunks[i]
            # reshape
            weight_chunk = weight_chunk.view(self.up_shape)
            # run a simple lenear layer with the down weight
            x_chunk = x_chunk @ weight_chunk.T
            x_out.append(x_chunk)
        x = torch.cat(x_out, dim=0)
        return x




class InstantLoRAModule(torch.nn.Module):
    def __init__(
            self,
            vision_hidden_size: int,
            vision_tokens: int,
            head_dim: int,
            sd: 'StableDiffusion'
    ):
        super(InstantLoRAModule, self).__init__()
        # self.linear = torch.nn.Linear(2, 1)
        self.sd_ref = weakref.ref(sd)
        self.dim = sd.network.lora_dim
        self.vision_hidden_size = vision_hidden_size
        self.vision_tokens = vision_tokens
        self.head_dim = head_dim

        # stores the projection vector. Grabbed by modules
        self.img_embeds: List[torch.Tensor] = None

        # disable merging in. It is slower on inference
        self.sd_ref().network.can_merge_in = False

        self.ilora_modules = torch.nn.ModuleList()

        lora_modules = self.sd_ref().network.get_all_modules()

        output_size = 0

        self.embed_lengths = []
        self.weight_mapping = []

        for idx, lora_module in enumerate(lora_modules):
            module_dict = lora_module.state_dict()
            down_shape = list(module_dict['lora_down.weight'].shape)
            up_shape = list(module_dict['lora_up.weight'].shape)

            self.weight_mapping.append([lora_module.lora_name, [down_shape, up_shape]])

            module_size = math.prod(down_shape) + math.prod(up_shape)
            output_size += module_size
            self.embed_lengths.append(module_size)


            # add a new mid module that will take the original forward and add a vector to it
            # this will be used to add the vector to the original forward
            instant_module = InstantLoRAMidModule(
                idx,
                lora_module,
                self,
                up_shape=up_shape,
                down_shape=down_shape
            )

            self.ilora_modules.append(instant_module)

            # replace the LoRA forwards
            lora_module.lora_down.forward = instant_module.down_forward
            lora_module.lora_up.forward = instant_module.up_forward


        self.output_size = output_size

        if vision_tokens > 1:
            self.resampler = Resampler(
                dim=vision_hidden_size,
                depth=4,
                dim_head=64,
                heads=12,
                num_queries=1,  # output tokens
                embedding_dim=vision_hidden_size,
                max_seq_len=vision_tokens,
                output_dim=head_dim,
                ff_mult=4
            )

        self.proj_module = LoRAGenerator(
            input_size=head_dim,
            hidden_size=head_dim,
            head_size=head_dim,
            num_mlp_layers=1,
            output_size=self.output_size,
        )

        self.migrate_weight_mapping()

    def migrate_weight_mapping(self):
        # changes the names of the modules to common ones
        keymap = self.sd_ref().network.get_keymap()
        save_keymap = {}
        if keymap is not None:
            for ldm_key, diffusers_key in keymap.items():
                #  invert them
                save_keymap[diffusers_key] = ldm_key

            new_keymap = {}
            for key, value in self.weight_mapping:
                if key in save_keymap:
                    new_keymap[save_keymap[key]] = value
                else:
                    print(f"Key {key} not found in keymap")
                    new_keymap[key] = value
            self.weight_mapping = new_keymap
        else:
            print("No keymap found. Using default names")
            return


    def forward(self, img_embeds):
        # expand token rank if only rank 2
        if len(img_embeds.shape) == 2:
            img_embeds = img_embeds.unsqueeze(1)

        # resample the image embeddings
        img_embeds = self.resampler(img_embeds)
        img_embeds = self.proj_module(img_embeds)
        if len(img_embeds.shape) == 3:
            img_embeds = img_embeds.squeeze(1)

        self.img_embeds = []
        # get all the slices
        start = 0
        for length in self.embed_lengths:
            self.img_embeds.append(img_embeds[:, start:start+length])
            start += length


    def get_additional_save_metadata(self) -> Dict[str, Any]:
        # save the weight mapping
        return {
            "weight_mapping": self.weight_mapping
        }

