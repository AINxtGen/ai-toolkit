import copy
import glob
import inspect
from collections import OrderedDict
import os
from typing import Union, List

import numpy as np
from diffusers import T2IAdapter
# from lycoris.config import PRESET
from torch.utils.data import DataLoader
import torch
import torch.backends.cuda

from toolkit.basic import value_map
from toolkit.data_loader import get_dataloader_from_datasets, trigger_dataloader_setup_epoch
from toolkit.data_transfer_object.data_loader import FileItemDTO, DataLoaderBatchDTO
from toolkit.embedding import Embedding
from toolkit.ip_adapter import IPAdapter
from toolkit.lora_special import LoRASpecialNetwork
from toolkit.lycoris_special import LycorisSpecialNetwork
from toolkit.network_mixins import Network
from toolkit.optimizer import get_optimizer
from toolkit.paths import CONFIG_ROOT
from toolkit.progress_bar import ToolkitProgressBar
from toolkit.sampler import get_sampler
from toolkit.saving import save_t2i_from_diffusers, load_t2i_model, save_ip_adapter_from_diffusers, \
    load_ip_adapter_model

from toolkit.scheduler import get_lr_scheduler
from toolkit.sd_device_states_presets import get_train_sd_device_state_preset
from toolkit.stable_diffusion_model import StableDiffusion

from jobs.process import BaseTrainProcess
from toolkit.metadata import get_meta_for_safetensors, load_metadata_from_safetensors, add_base_model_info_to_meta
from toolkit.train_tools import get_torch_dtype
import gc

from tqdm import tqdm

from toolkit.config_modules import SaveConfig, LogingConfig, SampleConfig, NetworkConfig, TrainConfig, ModelConfig, \
    GenerateImageConfig, EmbeddingConfig, DatasetConfig, preprocess_dataset_raw_config, AdapterConfig


def flush():
    torch.cuda.empty_cache()
    gc.collect()


class BaseSDTrainProcess(BaseTrainProcess):

    def __init__(self, process_id: int, job, config: OrderedDict, custom_pipeline=None):
        super().__init__(process_id, job, config)
        self.sd: StableDiffusion
        self.embedding: Union[Embedding, None] = None

        self.custom_pipeline = custom_pipeline
        self.step_num = 0
        self.start_step = 0
        self.device = self.get_conf('device', self.job.device)
        self.device_torch = torch.device(self.device)
        network_config = self.get_conf('network', None)
        if network_config is not None:
            self.network_config = NetworkConfig(**network_config)
        else:
            self.network_config = None
        self.train_config = TrainConfig(**self.get_conf('train', {}))
        self.model_config = ModelConfig(**self.get_conf('model', {}))
        self.save_config = SaveConfig(**self.get_conf('save', {}))
        self.sample_config = SampleConfig(**self.get_conf('sample', {}))
        first_sample_config = self.get_conf('first_sample', None)
        if first_sample_config is not None:
            self.has_first_sample_requested = True
            self.first_sample_config = SampleConfig(**first_sample_config)
        else:
            self.has_first_sample_requested = False
            self.first_sample_config = self.sample_config
        self.logging_config = LogingConfig(**self.get_conf('logging', {}))
        self.optimizer: torch.optim.Optimizer = None
        self.lr_scheduler = None
        self.data_loader: Union[DataLoader, None] = None
        self.data_loader_reg: Union[DataLoader, None] = None
        self.trigger_word = self.get_conf('trigger_word', None)

        # store is all are cached. Allows us to not load vae if we don't need to
        self.is_latents_cached = True
        raw_datasets = self.get_conf('datasets', None)
        if raw_datasets is not None and len(raw_datasets) > 0:
            raw_datasets = preprocess_dataset_raw_config(raw_datasets)
        self.datasets = None
        self.datasets_reg = None
        self.params = []
        if raw_datasets is not None and len(raw_datasets) > 0:
            for raw_dataset in raw_datasets:
                dataset = DatasetConfig(**raw_dataset)
                is_caching = dataset.cache_latents or dataset.cache_latents_to_disk
                if not is_caching:
                    self.is_latents_cached = False
                if dataset.is_reg:
                    if self.datasets_reg is None:
                        self.datasets_reg = []
                    self.datasets_reg.append(dataset)
                else:
                    if self.datasets is None:
                        self.datasets = []
                    self.datasets.append(dataset)

        self.embed_config = None
        embedding_raw = self.get_conf('embedding', None)
        if embedding_raw is not None:
            self.embed_config = EmbeddingConfig(**embedding_raw)

        # t2i adapter
        self.adapter_config = None
        adapter_raw = self.get_conf('adapter', None)
        if adapter_raw is not None:
            self.adapter_config = AdapterConfig(**adapter_raw)
            # sdxl adapters end in _xl. Only full_adapter_xl for now
            if self.model_config.is_xl and not self.adapter_config.adapter_type.endswith('_xl'):
                self.adapter_config.adapter_type += '_xl'

        # to hold network if there is one
        self.network: Union[Network, None] = None
        self.adapter: Union[T2IAdapter, IPAdapter, None] = None
        self.embedding: Union[Embedding, None] = None

        is_training_adapter = self.adapter_config is not None and self.adapter_config.train

        # get the device state preset based on what we are training
        self.train_device_state_preset = get_train_sd_device_state_preset(
            device=self.device_torch,
            train_unet=self.train_config.train_unet,
            train_text_encoder=self.train_config.train_text_encoder,
            cached_latents=self.is_latents_cached,
            train_lora=self.network_config is not None,
            train_adapter=is_training_adapter,
            train_embedding=self.embed_config is not None,
        )

        # fine_tuning here is for training actual SD network, not LoRA, embeddings, etc. it is (Dreambooth, etc)
        self.is_fine_tuning = True
        if self.network_config is not None or is_training_adapter or self.embed_config is not None:
            self.is_fine_tuning = False

        self.named_lora = False
        if self.embed_config is not None or is_training_adapter:
            self.named_lora = True

    def post_process_generate_image_config_list(self, generate_image_config_list: List[GenerateImageConfig]):
        # override in subclass
        return generate_image_config_list

    def sample(self, step=None, is_first=False):
        sample_folder = os.path.join(self.save_root, 'samples')
        gen_img_config_list = []

        sample_config = self.first_sample_config if is_first else self.sample_config
        start_seed = sample_config.seed
        current_seed = start_seed
        for i in range(len(sample_config.prompts)):
            if sample_config.walk_seed:
                current_seed = start_seed + i

            step_num = ''
            if step is not None:
                # zero-pad 9 digits
                step_num = f"_{str(step).zfill(9)}"

            filename = f"[time]_{step_num}_[count].{self.sample_config.ext}"

            output_path = os.path.join(sample_folder, filename)

            prompt = sample_config.prompts[i]

            # add embedding if there is one
            # note: diffusers will automatically expand the trigger to the number of added tokens
            # ie test123 will become test123 test123_1 test123_2 etc. Do not add this yourself here
            if self.embedding is not None:
                prompt = self.embedding.inject_embedding_to_prompt(
                    prompt, add_if_not_present=False
                )
            if self.trigger_word is not None:
                prompt = self.sd.inject_trigger_into_prompt(
                    prompt, self.trigger_word, add_if_not_present=False
                )

            extra_args = {}
            if self.adapter_config is not None and self.adapter_config.test_img_path is not None:
                extra_args['adapter_image_path'] = self.adapter_config.test_img_path

            gen_img_config_list.append(GenerateImageConfig(
                prompt=prompt,  # it will autoparse the prompt
                width=sample_config.width,
                height=sample_config.height,
                negative_prompt=sample_config.neg,
                seed=current_seed,
                guidance_scale=sample_config.guidance_scale,
                guidance_rescale=sample_config.guidance_rescale,
                num_inference_steps=sample_config.sample_steps,
                network_multiplier=sample_config.network_multiplier,
                output_path=output_path,
                output_ext=sample_config.ext,
                adapter_conditioning_scale=sample_config.adapter_conditioning_scale,
                **extra_args
            ))

        # post process
        gen_img_config_list = self.post_process_generate_image_config_list(gen_img_config_list)

        # send to be generated
        self.sd.generate_images(gen_img_config_list, sampler=sample_config.sampler)

    def update_training_metadata(self):
        o_dict = OrderedDict({
            "training_info": self.get_training_info()
        })
        if self.model_config.is_v2:
            o_dict['ss_v2'] = True
            o_dict['ss_base_model_version'] = 'sd_2.1'

        elif self.model_config.is_xl:
            o_dict['ss_base_model_version'] = 'sdxl_1.0'
        else:
            o_dict['ss_base_model_version'] = 'sd_1.5'

        o_dict = add_base_model_info_to_meta(
            o_dict,
            is_v2=self.model_config.is_v2,
            is_xl=self.model_config.is_xl,
        )
        o_dict['ss_output_name'] = self.job.name

        if self.trigger_word is not None:
            # just so auto1111 will pick it up
            o_dict['ss_tag_frequency'] = {
                f"1_{self.trigger_word}": {
                    f"{self.trigger_word}": 1
                }
            }

        self.add_meta(o_dict)

    def get_training_info(self):
        info = OrderedDict({
            'step': self.step_num + 1
        })
        return info

    def clean_up_saves(self):
        # remove old saves
        # get latest saved step
        if os.path.exists(self.save_root):
            latest_file = None
            # pattern is {job_name}_{zero_filles_step}.safetensors but NOT {job_name}.safetensors
            pattern = f"{self.job.name}_*.safetensors"
            files = glob.glob(os.path.join(self.save_root, pattern))
            if len(files) > self.save_config.max_step_saves_to_keep:
                # remove all but the latest max_step_saves_to_keep
                files.sort(key=os.path.getctime)
                for file in files[:-self.save_config.max_step_saves_to_keep]:
                    self.print(f"Removing old save: {file}")
                    os.remove(file)
                    # see if a yaml file with same name exists
                    yaml_file = os.path.splitext(file)[0] + ".yaml"
                    if os.path.exists(yaml_file):
                        os.remove(yaml_file)
            return latest_file
        else:
            return None

    def post_save_hook(self, save_path):
        # override in subclass
        pass

    def save(self, step=None):
        if not os.path.exists(self.save_root):
            os.makedirs(self.save_root, exist_ok=True)

        step_num = ''
        if step is not None:
            # zeropad 9 digits
            step_num = f"_{str(step).zfill(9)}"

        self.update_training_metadata()
        filename = f'{self.job.name}{step_num}.safetensors'
        file_path = os.path.join(self.save_root, filename)
        # prepare meta
        save_meta = get_meta_for_safetensors(self.meta, self.job.name)
        if not self.is_fine_tuning:
            if self.network is not None:
                lora_name = self.job.name
                if self.named_lora:
                    # add _lora to name
                    lora_name += '_LoRA'

                filename = f'{lora_name}{step_num}.safetensors'
                file_path = os.path.join(self.save_root, filename)
                prev_multiplier = self.network.multiplier
                self.network.multiplier = 1.0
                if self.network_config.normalize:
                    # apply the normalization
                    self.network.apply_stored_normalizer()

                # if we are doing embedding training as well, add that
                embedding_dict = self.embedding.state_dict() if self.embedding else None
                self.network.save_weights(
                    file_path,
                    dtype=get_torch_dtype(self.save_config.dtype),
                    metadata=save_meta,
                    extra_state_dict=embedding_dict
                )
                self.network.multiplier = prev_multiplier
                # if we have an embedding as well, pair it with the network

            # even if added to lora, still save the trigger version
            if self.embedding is not None:
                emb_filename = f'{self.embed_config.trigger}{step_num}.safetensors'
                emb_file_path = os.path.join(self.save_root, emb_filename)
                # for combo, above will get it
                # set current step
                self.embedding.step = self.step_num
                # change filename to pt if that is set
                if self.embed_config.save_format == "pt":
                    # replace extension
                    emb_file_path = os.path.splitext(emb_file_path)[0] + ".pt"
                self.embedding.save(emb_file_path)

            if self.adapter is not None and self.adapter_config.train:
                adapter_name = self.job.name
                if self.network_config is not None or self.embedding is not None:
                    # add _lora to name
                    if self.adapter_config.type == 't2i':
                        adapter_name += '_t2i'
                    else:
                        adapter_name += '_ip'

                filename = f'{adapter_name}{step_num}.safetensors'
                file_path = os.path.join(self.save_root, filename)
                # save adapter
                state_dict = self.adapter.state_dict()
                if self.adapter_config.type == 't2i':
                    save_t2i_from_diffusers(
                        state_dict,
                        output_file=file_path,
                        meta=save_meta,
                        dtype=get_torch_dtype(self.save_config.dtype)
                    )
                else:
                    save_ip_adapter_from_diffusers(
                        state_dict,
                        output_file=file_path,
                        meta=save_meta,
                        dtype=get_torch_dtype(self.save_config.dtype)
                    )
        else:
            self.sd.save(
                file_path,
                save_meta,
                get_torch_dtype(self.save_config.dtype)
            )

        self.print(f"Saved to {file_path}")
        self.clean_up_saves()
        self.post_save_hook(file_path)

    # Called before the model is loaded
    def hook_before_model_load(self):
        # override in subclass
        pass

    def hook_add_extra_train_params(self, params):
        # override in subclass
        return params

    def hook_before_train_loop(self):
        pass

    def before_dataset_load(self):
        pass

    def get_params(self):
        # you can extend this in subclass to get params
        # otherwise params will be gathered through normal means
        return None

    def hook_train_loop(self, batch):
        # return loss
        return 0.0

    def get_latest_save_path(self, name=None, post=''):
        if name == None:
            name = self.job.name
        # get latest saved step
        if os.path.exists(self.save_root):
            latest_file = None
            # pattern is {job_name}_{zero_filles_step}.safetensors or {job_name}.safetensors
            pattern = f"{name}*{post}.safetensors"
            files = glob.glob(os.path.join(self.save_root, pattern))
            if len(files) > 0:
                latest_file = max(files, key=os.path.getctime)
            # try pt
            pattern = f"{name}*.pt"
            files = glob.glob(os.path.join(self.save_root, pattern))
            if len(files) > 0:
                latest_file = max(files, key=os.path.getctime)
            return latest_file
        else:
            return None

    def load_training_state_from_metadata(self, path):
        meta = load_metadata_from_safetensors(path)
        # if 'training_info' in Orderdict keys
        if 'training_info' in meta and 'step' in meta['training_info'] and self.train_config.start_step is None:
            self.step_num = meta['training_info']['step']
            self.start_step = self.step_num
            print(f"Found step {self.step_num} in metadata, starting from there")

    def load_weights(self, path):
        if self.network is not None:
            extra_weights = self.network.load_weights(path)
            self.load_training_state_from_metadata(path)
            return extra_weights
        else:
            print("load_weights not implemented for non-network models")
            return None

    # def get_sigmas(self, timesteps, n_dim=4, dtype=torch.float32):
    #     self.sd.noise_scheduler.set_timesteps(1000, device=self.device_torch)
    #     sigmas = self.sd.noise_scheduler.sigmas.to(device=self.device_torch, dtype=dtype)
    #     schedule_timesteps = self.sd.noise_scheduler.timesteps.to(self.device_torch, )
    #     timesteps = timesteps.to(self.device_torch, )
    #
    #     # step_indices = [(schedule_timesteps == t).nonzero().item() for t in timesteps]
    #     step_indices = [t for t in timesteps]
    #
    #     sigma = sigmas[step_indices].flatten()
    #     while len(sigma.shape) < n_dim:
    #         sigma = sigma.unsqueeze(-1)
    #     return sigma

    def load_additional_training_modules(self, params):
        # override in subclass
        return params

    def get_sigmas(self, timesteps, n_dim=4, dtype=torch.float32):
        sigmas = self.sd.noise_scheduler.sigmas.to(device=self.device, dtype=dtype)
        schedule_timesteps = self.sd.noise_scheduler.timesteps.to(self.device)
        timesteps = timesteps.to(self.device)

        step_indices = [(schedule_timesteps == t).nonzero().item() for t in timesteps]

        sigma = sigmas[step_indices].flatten()
        while len(sigma.shape) < n_dim:
            sigma = sigma.unsqueeze(-1)
        return sigma

    def process_general_training_batch(self, batch: 'DataLoaderBatchDTO'):
        with torch.no_grad():
            with self.timer('prepare_prompt'):
                prompts = batch.get_caption_list()
                is_reg_list = batch.get_is_reg_list()

                conditioned_prompts = []

                for prompt, is_reg in zip(prompts, is_reg_list):

                    # make sure the embedding is in the prompts
                    if self.embedding is not None:
                        prompt = self.embedding.inject_embedding_to_prompt(
                            prompt,
                            expand_token=True,
                            add_if_not_present=not is_reg,
                        )

                    # make sure trigger is in the prompts if not a regularization run
                    if self.trigger_word is not None:
                        prompt = self.sd.inject_trigger_into_prompt(
                            prompt,
                            trigger=self.trigger_word,
                            add_if_not_present=not is_reg,
                        )
                    conditioned_prompts.append(prompt)

            with self.timer('prepare_latents'):
                dtype = get_torch_dtype(self.train_config.dtype)
                imgs = None
                if batch.tensor is not None:
                    imgs = batch.tensor
                    imgs = imgs.to(self.device_torch, dtype=dtype)
                if batch.latents is not None:
                    latents = batch.latents.to(self.device_torch, dtype=dtype)
                    batch.latents = latents
                else:
                    latents = self.sd.encode_images(imgs)
                    batch.latents = latents
                    # flush()  # todo check performance removing this

                unaugmented_latents = None
                if self.train_config.loss_target == 'differential_noise':
                    # we determine noise from the differential of the latents
                    unaugmented_latents = self.sd.encode_images(batch.unaugmented_tensor)

            batch_size = latents.shape[0]

            with self.timer('prepare_noise'):

                self.sd.noise_scheduler.set_timesteps(
                    1000, device=self.device_torch
                )

                # if self.train_config.timestep_sampling == 'style' or self.train_config.timestep_sampling == 'content':
                if self.train_config.content_or_style in ['style', 'content']:
                    # this is from diffusers training code
                    # Cubic sampling for favoring later or earlier timesteps
                    # For more details about why cubic sampling is used for content / structure,
                    # refer to section 3.4 of https://arxiv.org/abs/2302.08453

                    # for content / structure, it is best to favor earlier timesteps
                    # for style, it is best to favor later timesteps

                    timesteps = torch.rand((batch_size,), device=latents.device)

                    if self.train_config.content_or_style == 'style':
                        timesteps = timesteps ** 3 * self.sd.noise_scheduler.config['num_train_timesteps']
                    elif self.train_config.content_or_style == 'content':
                        timesteps = (1 - timesteps ** 3) * self.sd.noise_scheduler.config['num_train_timesteps']

                    timesteps = value_map(
                        timesteps,
                        0,
                        self.sd.noise_scheduler.config['num_train_timesteps'] - 1,
                        self.train_config.min_denoising_steps,
                        self.train_config.max_denoising_steps
                    )
                    timesteps = timesteps.long().clamp(
                        self.train_config.min_denoising_steps + 1,
                        self.train_config.max_denoising_steps - 1
                    )

                elif self.train_config.content_or_style == 'balanced':
                    timesteps = torch.randint(
                        self.train_config.min_denoising_steps,
                        self.train_config.max_denoising_steps,
                        (batch_size,),
                        device=self.device_torch
                    )
                    timesteps = timesteps.long()
                else:
                    raise ValueError(f"Unknown content_or_style {self.train_config.content_or_style}")

                # get noise
                noise = self.sd.get_latent_noise(
                    height=latents.shape[2],
                    width=latents.shape[3],
                    batch_size=batch_size,
                    noise_offset=self.train_config.noise_offset
                ).to(self.device_torch, dtype=dtype)

                if self.train_config.loss_target == 'differential_noise':
                    differential = latents - unaugmented_latents
                    # add noise to differential
                    # noise = noise + differential
                    noise = noise + (differential * 0.5)
                    # noise = value_map(differential, 0, torch.abs(differential).max(), 0, torch.abs(noise).max())
                    latents = unaugmented_latents

                noise_multiplier = self.train_config.noise_multiplier

                noise = noise * noise_multiplier

                img_multiplier = self.train_config.img_multiplier

                latents = latents * img_multiplier

                noisy_latents = self.sd.noise_scheduler.add_noise(latents, noise, timesteps)

                # https://github.com/huggingface/diffusers/blob/324d18fba23f6c9d7475b0ff7c777685f7128d40/examples/t2i_adapter/train_t2i_adapter_sdxl.py#L1170C17-L1171C77
                if self.train_config.loss_target == 'source' or self.train_config.loss_target == 'unaugmented':
                    sigmas = self.get_sigmas(timesteps, len(noisy_latents.shape), noisy_latents.dtype)
                    # add it to the batch
                    batch.sigmas = sigmas
                    # todo is this for sdxl? find out where this came from originally
                    # noisy_latents = noisy_latents / ((sigmas ** 2 + 1) ** 0.5)

            # remove grads for these
            noisy_latents.requires_grad = False
            noisy_latents = noisy_latents.detach()
            noise.requires_grad = False
            noise = noise.detach()

        return noisy_latents, noise, timesteps, conditioned_prompts, imgs

    def setup_adapter(self):
        # t2i adapter
        is_t2i = self.adapter_config.type == 't2i'
        suffix = 't2i' if is_t2i else 'ip'
        adapter_name = self.name
        if self.network_config is not None:
            adapter_name = f"{adapter_name}_{suffix}"
        latest_save_path = self.get_latest_save_path(adapter_name)

        dtype = get_torch_dtype(self.train_config.dtype)
        if is_t2i:
            # if we do not have a last save path and we have a name_or_path,
            # load from that
            if latest_save_path is None and self.adapter_config.name_or_path is not None:
                self.adapter = T2IAdapter.from_pretrained(
                    self.adapter_config.name_or_path,
                    torch_dtype=get_torch_dtype(self.train_config.dtype),
                    varient="fp16",
                    # use_safetensors=True,
                )
            else:
                self.adapter = T2IAdapter(
                    in_channels=self.adapter_config.in_channels,
                    channels=self.adapter_config.channels,
                    num_res_blocks=self.adapter_config.num_res_blocks,
                    downscale_factor=self.adapter_config.downscale_factor,
                    adapter_type=self.adapter_config.adapter_type,
                )
        else:
            self.adapter = IPAdapter(
                sd=self.sd,
                adapter_config=self.adapter_config,
            )
        self.adapter.to(self.device_torch, dtype=dtype)
        if latest_save_path is not None:
            # load adapter from path
            print(f"Loading adapter from {latest_save_path}")
            if is_t2i:
                loaded_state_dict = load_t2i_model(
                    latest_save_path,
                    self.device,
                    dtype=dtype
                )
            else:
                loaded_state_dict = load_ip_adapter_model(
                    latest_save_path,
                    self.device,
                    dtype=dtype
                )
            self.adapter.load_state_dict(loaded_state_dict)
            if self.adapter_config.train:
                self.load_training_state_from_metadata(latest_save_path)
        # set trainable params
        self.sd.adapter = self.adapter

    def run(self):
        # torch.autograd.set_detect_anomaly(True)
        # run base process run
        BaseTrainProcess.run(self)

        ### HOOK ###
        self.hook_before_model_load()
        model_config_to_load = copy.deepcopy(self.model_config)

        if self.is_fine_tuning:
            # get the latest checkpoint
            # check to see if we have a latest save
            latest_save_path = self.get_latest_save_path()

            if latest_save_path is not None:
                print(f"#### IMPORTANT RESUMING FROM {latest_save_path} ####")
                model_config_to_load.name_or_path = latest_save_path
                meta = load_metadata_from_safetensors(latest_save_path)
                # if 'training_info' in Orderdict keys
                if 'training_info' in meta and 'step' in meta['training_info']:
                    self.step_num = meta['training_info']['step']
                    self.start_step = self.step_num
                    print(f"Found step {self.step_num} in metadata, starting from there")

        # get the noise scheduler
        sampler = get_sampler(self.train_config.noise_scheduler)

        self.sd = StableDiffusion(
            device=self.device,
            model_config=model_config_to_load,
            dtype=self.train_config.dtype,
            custom_pipeline=self.custom_pipeline,
            noise_scheduler=sampler,
        )
        # run base sd process run
        self.sd.load_model()

        dtype = get_torch_dtype(self.train_config.dtype)

        # model is loaded from BaseSDProcess
        unet = self.sd.unet
        vae = self.sd.vae
        tokenizer = self.sd.tokenizer
        text_encoder = self.sd.text_encoder
        noise_scheduler = self.sd.noise_scheduler

        if self.train_config.xformers:
            vae.enable_xformers_memory_efficient_attention()
            unet.enable_xformers_memory_efficient_attention()
            if isinstance(text_encoder, list):
                for te in text_encoder:
                    # if it has it
                    if hasattr(te, 'enable_xformers_memory_efficient_attention'):
                        te.enable_xformers_memory_efficient_attention()
        if self.train_config.sdp:
            torch.backends.cuda.enable_math_sdp(True)
            torch.backends.cuda.enable_flash_sdp(True)
            torch.backends.cuda.enable_mem_efficient_sdp(True)

        if self.train_config.gradient_checkpointing:
            unet.enable_gradient_checkpointing()
            if isinstance(text_encoder, list):
                for te in text_encoder:
                    if hasattr(te, 'enable_gradient_checkpointing'):
                        te.enable_gradient_checkpointing()
                    if hasattr(te, "gradient_checkpointing_enable"):
                        te.gradient_checkpointing_enable()
            else:
                if hasattr(text_encoder, 'enable_gradient_checkpointing'):
                    text_encoder.enable_gradient_checkpointing()
                if hasattr(text_encoder, "gradient_checkpointing_enable"):
                    text_encoder.gradient_checkpointing_enable()

        if isinstance(text_encoder, list):
            for te in text_encoder:
                te.requires_grad_(False)
                te.eval()
        else:
            text_encoder.requires_grad_(False)
            text_encoder.eval()
        unet.to(self.device_torch, dtype=dtype)
        unet.requires_grad_(False)
        unet.eval()
        vae = vae.to(torch.device('cpu'), dtype=dtype)
        vae.requires_grad_(False)
        vae.eval()
        flush()

        ### HOOk ###
        self.before_dataset_load()
        # load datasets if passed in the root process
        if self.datasets is not None:
            self.data_loader = get_dataloader_from_datasets(self.datasets, self.train_config.batch_size, self.sd)
        if self.datasets_reg is not None:
            self.data_loader_reg = get_dataloader_from_datasets(self.datasets_reg, self.train_config.batch_size,
                                                                self.sd)
        params = []
        if not self.is_fine_tuning:
            if self.network_config is not None:
                # TODO should we completely switch to LycorisSpecialNetwork?

                is_lycoris = False
                # default to LoCON if there are any conv layers or if it is named
                NetworkClass = LoRASpecialNetwork
                if self.network_config.type.lower() == 'locon' or self.network_config.type.lower() == 'lycoris':
                    NetworkClass = LycorisSpecialNetwork
                    is_lycoris = True

                # if is_lycoris:
                #     preset = PRESET['full']
                # NetworkClass.apply_preset(preset)

                self.network = NetworkClass(
                    text_encoder=text_encoder,
                    unet=unet,
                    lora_dim=self.network_config.linear,
                    multiplier=1.0,
                    alpha=self.network_config.linear_alpha,
                    train_unet=self.train_config.train_unet,
                    train_text_encoder=self.train_config.train_text_encoder,
                    conv_lora_dim=self.network_config.conv,
                    conv_alpha=self.network_config.conv_alpha,
                    is_sdxl=self.model_config.is_xl,
                    is_v2=self.model_config.is_v2,
                    dropout=self.network_config.dropout,
                    use_text_encoder_1=self.model_config.use_text_encoder_1,
                    use_text_encoder_2=self.model_config.use_text_encoder_2,
                )

                self.network.force_to(self.device_torch, dtype=dtype)
                # give network to sd so it can use it
                self.sd.network = self.network
                self.network._update_torch_multiplier()

                self.network.apply_to(
                    text_encoder,
                    unet,
                    self.train_config.train_text_encoder,
                    self.train_config.train_unet
                )

                self.network.prepare_grad_etc(text_encoder, unet)
                flush()

                # LyCORIS doesnt have default_lr
                config = {
                    'text_encoder_lr': self.train_config.lr,
                    'unet_lr': self.train_config.lr,
                }
                sig = inspect.signature(self.network.prepare_optimizer_params)
                if 'default_lr' in sig.parameters:
                    config['default_lr'] = self.train_config.lr
                if 'learning_rate' in sig.parameters:
                    config['learning_rate'] = self.train_config.lr
                params_net = self.network.prepare_optimizer_params(
                    **config
                )

                params += params_net

                if self.train_config.gradient_checkpointing:
                    self.network.enable_gradient_checkpointing()

                # set the network to normalize if we are
                self.network.is_normalizing = self.network_config.normalize

                lora_name = self.name
                # need to adapt name so they are not mixed up
                if self.named_lora:
                    lora_name = f"{lora_name}_LoRA"

                latest_save_path = self.get_latest_save_path(lora_name)
                extra_weights = None
                if latest_save_path is not None:
                    self.print(f"#### IMPORTANT RESUMING FROM {latest_save_path} ####")
                    self.print(f"Loading from {latest_save_path}")
                    extra_weights = self.load_weights(latest_save_path)
                    self.network.multiplier = 1.0

            if self.embed_config is not None:
                # we are doing embedding training as well
                self.embedding = Embedding(
                    sd=self.sd,
                    embed_config=self.embed_config
                )
                latest_save_path = self.get_latest_save_path(self.embed_config.trigger)
                # load last saved weights
                if latest_save_path is not None:
                    self.embedding.load_embedding_from_file(latest_save_path, self.device_torch)
                params.append({
                    'params': self.embedding.get_trainable_params(),
                    'lr': self.train_config.embedding_lr
                })

                flush()

            if self.embed_config is not None:
                self.embedding = Embedding(
                    sd=self.sd,
                    embed_config=self.embed_config
                )
                latest_save_path = self.get_latest_save_path(self.embed_config.trigger)
                # load last saved weights
                if latest_save_path is not None:
                    self.embedding.load_embedding_from_file(latest_save_path, self.device_torch)

                # resume state from embedding
                self.step_num = self.embedding.step
                self.start_step = self.step_num

                params = self.get_params()
                if not params:
                    # set trainable params
                    params = self.embedding.get_trainable_params()

                flush()

            if self.adapter_config is not None:
                self.setup_adapter()
                # set trainable params
                params.append({
                    'params': self.adapter.parameters(),
                    'lr': self.train_config.adapter_lr
                })
                flush()

            params = self.load_additional_training_modules(params)

        else:  # no network, embedding or adapter
            # set the device state preset before getting params
            self.sd.set_device_state(self.train_device_state_preset)

            params = self.get_params()
            if not params:
                # will only return savable weights and ones with grad
                params = self.sd.prepare_optimizer_params(
                    unet=self.train_config.train_unet,
                    text_encoder=self.train_config.train_text_encoder,
                    text_encoder_lr=self.train_config.lr,
                    unet_lr=self.train_config.lr,
                    default_lr=self.train_config.lr
                )
            # we may be using it for prompt injections
            if self.adapter_config is not None:
                self.setup_adapter()
        flush()
        ### HOOK ###
        params = self.hook_add_extra_train_params(params)
        self.params = []

        for param in params:
            if isinstance(param, dict):
                self.params += param['params']
            else:
                self.params.append(param)

        if self.train_config.start_step is not None:
            self.step_num = self.train_config.start_step
            self.start_step = self.step_num

        optimizer_type = self.train_config.optimizer.lower()
        optimizer = get_optimizer(self.params, optimizer_type, learning_rate=self.train_config.lr,
                                  optimizer_params=self.train_config.optimizer_params)
        self.optimizer = optimizer

        lr_scheduler_params = self.train_config.lr_scheduler_params

        # make sure it had bare minimum
        if 'max_iterations' not in lr_scheduler_params:
            lr_scheduler_params['total_iters'] = self.train_config.steps

        lr_scheduler = get_lr_scheduler(
            self.train_config.lr_scheduler,
            optimizer,
            **lr_scheduler_params
        )
        self.lr_scheduler = lr_scheduler

        flush()
        ### HOOK ###
        self.hook_before_train_loop()

        if self.has_first_sample_requested:
            self.print("Generating first sample from first sample config")
            self.sample(0, is_first=True)

        # sample first
        if self.train_config.skip_first_sample:
            self.print("Skipping first sample due to config setting")
        else:
            self.print("Generating baseline samples before training")
            self.sample(0)

        self.progress_bar = ToolkitProgressBar(
            total=self.train_config.steps,
            desc=self.job.name,
            leave=True,
            initial=self.step_num,
            iterable=range(0, self.train_config.steps),
        )
        self.progress_bar.pause()

        if self.data_loader is not None:
            dataloader = self.data_loader
            dataloader_iterator = iter(dataloader)
        else:
            dataloader = None
            dataloader_iterator = None

        if self.data_loader_reg is not None:
            dataloader_reg = self.data_loader_reg
            dataloader_iterator_reg = iter(dataloader_reg)
        else:
            dataloader_reg = None
            dataloader_iterator_reg = None

        # zero any gradients
        optimizer.zero_grad()

        self.lr_scheduler.step(self.step_num)

        self.sd.set_device_state(self.train_device_state_preset)
        flush()
        # self.step_num = 0
        for step in range(self.step_num, self.train_config.steps):
            if self.train_config.free_u:
                self.sd.pipeline.enable_freeu(s1=0.9, s2=0.2, b1=1.1, b2=1.2)
            self.progress_bar.unpause()
            with torch.no_grad():
                # if is even step and we have a reg dataset, use that
                # todo improve this logic to send one of each through if we can buckets and batch size might be an issue
                is_reg_step = False
                is_save_step = self.save_config.save_every and self.step_num % self.save_config.save_every == 0
                is_sample_step = self.sample_config.sample_every and self.step_num % self.sample_config.sample_every == 0
                # don't do a reg step on sample or save steps as we dont want to normalize on those
                if step % 2 == 0 and dataloader_reg is not None and not is_save_step and not is_sample_step:
                    try:
                        with self.timer('get_batch:reg'):
                            batch = next(dataloader_iterator_reg)
                    except StopIteration:
                        with self.timer('reset_batch:reg'):
                            # hit the end of an epoch, reset
                            self.progress_bar.pause()
                            dataloader_iterator_reg = iter(dataloader_reg)
                            trigger_dataloader_setup_epoch(dataloader_reg)

                        with self.timer('get_batch:reg'):
                            batch = next(dataloader_iterator_reg)
                        self.progress_bar.unpause()
                    is_reg_step = True
                elif dataloader is not None:
                    try:
                        with self.timer('get_batch'):
                            batch = next(dataloader_iterator)
                    except StopIteration:
                        with self.timer('reset_batch'):
                            # hit the end of an epoch, reset
                            self.progress_bar.pause()
                            dataloader_iterator = iter(dataloader)
                            trigger_dataloader_setup_epoch(dataloader)
                        with self.timer('get_batch'):
                            batch = next(dataloader_iterator)
                        self.progress_bar.unpause()
                else:
                    batch = None

                # turn on normalization if we are using it and it is not on
                if self.network is not None and self.network_config.normalize and not self.network.is_normalizing:
                    self.network.is_normalizing = True
            # flush()
            ### HOOK ###
            self.timer.start('train_loop')
            loss_dict = self.hook_train_loop(batch)
            self.timer.stop('train_loop')
            # flush()
            # setup the networks to gradient checkpointing and everything works

            with torch.no_grad():
                # torch.cuda.empty_cache()
                if self.train_config.optimizer.lower().startswith('dadaptation') or \
                        self.train_config.optimizer.lower().startswith('prodigy'):
                    learning_rate = (
                            optimizer.param_groups[0]["d"] *
                            optimizer.param_groups[0]["lr"]
                    )
                else:
                    learning_rate = optimizer.param_groups[0]['lr']

                prog_bar_string = f"lr: {learning_rate:.1e}"
                for key, value in loss_dict.items():
                    prog_bar_string += f" {key}: {value:.3e}"

                self.progress_bar.set_postfix_str(prog_bar_string)

                # apply network normalizer if we are using it, not on regularization steps
                if self.network is not None and self.network.is_normalizing and not is_reg_step:
                    with self.timer('apply_normalizer'):
                        self.network.apply_stored_normalizer()

                # if the batch is a DataLoaderBatchDTO, then we need to clean it up
                if isinstance(batch, DataLoaderBatchDTO):
                    with self.timer('batch_cleanup'):
                        batch.cleanup()

                # don't do on first step
                if self.step_num != self.start_step:
                    if is_sample_step:
                        self.progress_bar.pause()
                        flush()
                        # print above the progress bar
                        if self.train_config.free_u:
                            self.sd.pipeline.disable_freeu()
                        self.sample(self.step_num)
                        self.progress_bar.unpause()

                    if is_save_step:
                        # print above the progress bar
                        self.progress_bar.pause()
                        self.print(f"Saving at step {self.step_num}")
                        self.save(self.step_num)
                        self.progress_bar.unpause()

                    if self.logging_config.log_every and self.step_num % self.logging_config.log_every == 0:
                        self.progress_bar.pause()
                        with self.timer('log_to_tensorboard'):
                            # log to tensorboard
                            if self.writer is not None:
                                for key, value in loss_dict.items():
                                    self.writer.add_scalar(f"{key}", value, self.step_num)
                                self.writer.add_scalar(f"lr", learning_rate, self.step_num)
                            self.progress_bar.unpause()

                    if self.performance_log_every > 0 and self.step_num % self.performance_log_every == 0:
                        self.progress_bar.pause()
                        # print the timers and clear them
                        self.timer.print()
                        self.timer.reset()
                        self.progress_bar.unpause()

                # sets progress bar to match out step
                self.progress_bar.update(step - self.progress_bar.n)
                # end of step
                self.step_num = step

                # flush every 10 steps
                # if self.step_num % 10 == 0:
                #     flush()

        self.progress_bar.close()
        if self.train_config.free_u:
            self.sd.pipeline.disable_freeu()
        self.sample(self.step_num + 1)
        print("")
        self.save()

        del (
            self.sd,
            unet,
            noise_scheduler,
            optimizer,
            self.network,
            tokenizer,
            text_encoder,
        )

        flush()
