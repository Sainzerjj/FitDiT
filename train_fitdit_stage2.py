#!/usr/bin/env python
# coding=utf-8
# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and

import argparse
import copy
import functools
import logging
import math
import os
import random
import shutil
import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import DistributedType, ProjectConfiguration, set_seed
from packaging import version
from PIL import Image
from torchvision import transforms
from tqdm.auto import tqdm
from omegaconf import OmegaConf
import diffusers
from diffusers import (
    AutoencoderKL,
    FlowMatchEulerDiscreteScheduler,
    FluxTransformer2DModel,
)
from diffusers.optimization import get_scheduler
from diffusers.pipelines.flux.pipeline_flux_controlnet import FluxControlNetPipeline
from diffusers.training_utils import compute_density_for_timestep_sampling, compute_loss_weighting_for_sd3
from diffusers.utils import check_min_version, is_wandb_available, make_image_grid
from diffusers.utils.import_utils import is_torch_npu_available, is_xformers_available
from diffusers.utils.torch_utils import is_compiled_module


from src.pipeline_stable_diffusion_3_tryon import StableDiffusion3TryOnPipeline
from src.transformer_sd3_garm import SD3Transformer2DModel as SD3Transformer2DModel_Garm
from src.transformer_sd3_vton import SD3Transformer2DModel as SD3Transformer2DModel_Vton
from src.pose_guider import PoseGuider
from diffusers.models.autoencoders import AutoencoderKL
from src.pipeline_stable_diffusion_3_tryon import StableDiffusion3TryOnPipeline
import torch.nn as nn
from datasets import DenosingDitDataset
from IPython import embed
import torch.nn.functional as F  

# Will error if the minimal version of diffusers is not installed. Remove at your own risks.
# check_min_version("0.33.0.dev0")
logger = get_logger(__name__)

class Net(nn.Module):
    def __init__(
        self,
        transformer_vton: SD3Transformer2DModel_Vton,
        pose_guider: PoseGuider,
    ):
        super().__init__()
        self.transformer_vton = transformer_vton
        self.pose_guider = pose_guider
    
    def forward(
        self,
        hidden_states,
        timesteps,
        pooled_projections,
        pose_image,
        ref_key,
        ref_value,
        encoder_hidden_states=None,
        return_dict=False,
    ):  
        pose_fea = self.pose_guider(pose_image)
        noise_pred = self.transformer_vton(
            hidden_states,
            timestep=timesteps,
            pooled_projections=pooled_projections,
            encoder_hidden_states=encoder_hidden_states,
            ref_key=ref_key,
            ref_value=ref_value,
            return_dict=return_dict,
            pose_cond=pose_fea
        )[0]
        return noise_pred
        

def main(cfg):
    if torch.backends.mps.is_available() and cfg.mixed_precision == "bf16":
        # due to pytorch#99272, MPS does not yet support bfloat16.
        raise ValueError(
            "Mixed precision training with bfloat16 is not supported on MPS. Please use fp16 (recommended) or fp32 instead."
        )

    accelerator_project_config = ProjectConfiguration(project_dir=cfg.output_dir)

    accelerator = Accelerator(
        gradient_accumulation_steps=cfg.gradient_accumulation_steps,
        mixed_precision=cfg.mixed_precision,
        project_config=accelerator_project_config,
    )

    # Disable AMP for MPS. A technique for accelerating machine learning computations on iOS and macOS devices.
    if torch.backends.mps.is_available():
        print("MPS is enabled. Disabling AMP.")
        accelerator.native_amp = False

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        # DEBUG, INFO, WARNING, ERROR, CRITICAL
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)

    if accelerator.is_local_main_process:
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if cfg.seed is not None:
        set_seed(cfg.seed)

    # Handle the repository creation
    if accelerator.is_main_process:
        if cfg.output_dir is not None:
            os.makedirs(cfg.output_dir, exist_ok=True)
            
            
    #初始化模型（VAE、文本编码器、条件编码器、主干网络等）
    transformer_garm = SD3Transformer2DModel_Garm.from_pretrained(os.path.join(cfg.model_root, "transformer_garm"))
    transformer_vton = SD3Transformer2DModel_Vton.from_pretrained(os.path.join(cfg.model_root, "transformer_vton"))
    vae = AutoencoderKL.from_pretrained(os.path.join(cfg.model_root, "vae"))
    pose_guider = PoseGuider(conditioning_embedding_channels=1536, conditioning_channels=3, block_out_channels=(32, 64, 256, 512))
    pose_guider.load_state_dict(torch.load(os.path.join(cfg.model_root, "pose_guider", "diffusion_pytorch_model.bin")))
    # image_encoder_large = CLIPVisionModelWithProjection.from_pretrained(os.path.join(cfg.model_root, "clip-vit-large-patch14"))
    # image_encoder_bigG = CLIPVisionModelWithProjection.from_pretrained("laion/CLIP-ViT-bigG-14-laion2B-39B-b160k")
    
    logger.info("all models loaded successfully")
    
    # 加载噪声调度器
    noise_scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(
        cfg.model_root,
        subfolder="scheduler",
    )
    
    # 设置模型梯度
    transformer_vton.requires_grad_(True)
    pose_guider.requires_grad_(True)
    
    vae.requires_grad_(False)
    # image_encoder_large.requires_grad_(False)
    # image_encoder_bigG.requires_grad_(False)
    transformer_garm.requires_grad_(False)

    def unwrap_model(model):
        model = accelerator.unwrap_model(model)
        model = model._orig_mod if is_compiled_module(model) else model
        return model

    if cfg.enable_xformers_memory_efficient_attention:
        if is_xformers_available():
            import xformers

            xformers_version = version.parse(xformers.__version__)
            if xformers_version == version.parse("0.0.16"):
                logger.warning(
                    "xFormers 0.0.16 cannot be used for training in some GPUs. If you observe problems during training, please update xFormers to at least 0.0.17. See https://huggingface.co/docs/diffusers/main/en/optimization/xformers for more details."
                )
            transformer_vton.enable_xformers_memory_efficient_attention()
        else:
            raise ValueError("xformers is not available. Make sure it is installed correctly")

    if cfg.gradient_checkpointing:
        transformer_vton.enable_gradient_checkpointing()
        
    # Check that all trainable models are in full precision
    low_precision_error_string = (
        " Please make sure to always have all model weights in full float32 precision when starting training - even if"
        " doing mixed precision training, copy of the weights should still be float32."
    )

    if unwrap_model(transformer_garm).dtype != torch.float32:
        raise ValueError(
            f"Controlnet loaded as datatype {unwrap_model(transformer_garm).dtype}. {low_precision_error_string}"
        )
    
    if unwrap_model(transformer_vton).dtype != torch.float32:
        raise ValueError(
            f"Controlnet loaded as datatype {unwrap_model(transformer_vton).dtype}. {low_precision_error_string}"
        )

    if cfg.scale_lr:
        cfg.learning_rate = (
            cfg.learning_rate * cfg.gradient_accumulation_steps * cfg.train_batch_size * accelerator.num_processes
        )
    
    # 设置weight_dtype
    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    net = Net(
        transformer_vton=transformer_vton,
        pose_guider=pose_guider,
    )
    
    vae.to(accelerator.device, dtype=weight_dtype)
    # image_encoder_large.to(accelerator.device, dtype=weight_dtype)
    # image_encoder_bigG.to(accelerator.device, dtype=weight_dtype)
    transformer_garm.to(accelerator.device, dtype=weight_dtype)
    
    transformer_vton.to(accelerator.device)
    pose_guider.to(accelerator.device)
    
    # Optimizer creation
    params_to_optimize = list(filter(lambda p: p.requires_grad, net.parameters()))
    
    # 定义优化器
    if cfg.use_8bit_adam:
        try:
            import bitsandbytes as bnb
        except ImportError:
            raise ImportError(
                "To use 8-bit Adam, please install the bitsandbytes library: `pip install bitsandbytes`."
            )

        optimizer_class = bnb.optim.AdamW8bit
    else:
        optimizer_class = torch.optim.AdamW
    # use adafactor optimizer to save gpu memory
    if cfg.use_adafactor:
        from transformers import Adafactor

        optimizer = Adafactor(
            params_to_optimize,
            lr=cfg.learning_rate,
            scale_parameter=False,
            relative_step=False,
            # warmup_init=True,
            weight_decay=cfg.adam_weight_decay,
        )
    else:
        optimizer = optimizer_class(
            params_to_optimize,
            lr=cfg.learning_rate,
            betas=(cfg.adam_beta1, cfg.adam_beta2),
            weight_decay=cfg.adam_weight_decay,
            eps=cfg.adam_epsilon,
        )

    # 定义数据集
    train_dataset = DenosingDitDataset(cfg.data.image_list, cfg.data.width, cfg.data.height)
   
    # Then get the training dataset ready to be passed to the dataloader.
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        shuffle=True,
        batch_size=cfg.data.train_batch_size,
        num_workers=cfg.data.dataloader_num_workers,
        pin_memory=False,
    )

    lr_scheduler = get_scheduler(
        cfg.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=cfg.lr_warmup_steps * accelerator.num_processes,
        num_training_steps=cfg.max_train_steps * accelerator.num_processes,
    )
    # Prepare everything with our `accelerator`.
    net, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
        net, optimizer, train_dataloader, lr_scheduler
    )

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / cfg.gradient_accumulation_steps)
    # Afterwards we recalculate our number of training epochs
    cfg.num_train_epochs = math.ceil(cfg.max_train_steps / num_update_steps_per_epoch)

    # Train!
    total_batch_size = cfg.data.train_batch_size * accelerator.num_processes * cfg.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num batches each epoch = {len(train_dataloader)}")
    logger.info(f"  Num Epochs = {cfg.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {cfg.data.train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {cfg.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {cfg.max_train_steps}")
    global_step = 0
    first_epoch = 0

    # Potentially load in the weights and states from a previous save
    if cfg.resume_from_checkpoint:
        if cfg.resume_from_checkpoint != "latest":
            path = os.path.basename(cfg.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = os.listdir(cfg.output_dir)
            dirs = [d for d in dirs if d.startswith("checkpoint")]
            dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
            path = dirs[-1] if len(dirs) > 0 else None

        if path is None:
            accelerator.print(
                f"Checkpoint '{cfg.resume_from_checkpoint}' does not exist. Starting a new training run."
            )
            cfg.resume_from_checkpoint = None
            initial_global_step = 0
        else:
            accelerator.print(f"Resuming from checkpoint {path}")
            accelerator.load_state(os.path.join(cfg.output_dir, path))
            global_step = int(path.split("-")[1])

            initial_global_step = global_step
            first_epoch = global_step // num_update_steps_per_epoch
    else:
        initial_global_step = 0

    progress_bar = tqdm(
        range(0, cfg.max_train_steps),
        initial=initial_global_step,
        desc="Steps",
        # Only show the progress bar once on each machine.
        disable=not accelerator.is_local_main_process,
    )

    def get_sigmas(timesteps, n_dim=4, dtype=torch.float32):
        sigmas = noise_scheduler.sigmas.to(device=accelerator.device, dtype=dtype)
        schedule_timesteps = noise_scheduler.timesteps.to(accelerator.device)
        timesteps = timesteps.to(accelerator.device)
        step_indices = [(schedule_timesteps == t).nonzero().item() for t in timesteps]

        sigma = sigmas[step_indices].flatten()
        while len(sigma.shape) < n_dim:
            sigma = sigma.unsqueeze(-1)
        return sigma
    for epoch in range(first_epoch, cfg.num_train_epochs):
        train_loss = 0.0
        for step, batch in enumerate(train_dataloader):
            with accelerator.accumulate(net):
                # Convert images to latent space
                # vae encode
                pixel_values = batch["vton_image"].to(dtype=weight_dtype)
                pixel_latents = vae.encode(pixel_values).latent_dist.sample()
                pixel_latents = (pixel_latents - vae.config.shift_factor) * vae.config.scaling_factor
                pixel_latents = pixel_latents.to(dtype=weight_dtype)
                
                bsz = pixel_latents.shape[0]
                # pixel_latents = _pack_latents(
                #     pixel_latents,
                #     pixel_values.shape[0],
                #     pixel_latents.shape[1],
                #     pixel_latents.shape[2],
                #     pixel_latents.shape[3],
                # )
                
                noise = torch.randn_like(pixel_latents).to(accelerator.device).to(dtype=weight_dtype)
                # Sample a random timestep for each image
                # for weighting schemes where we sample timesteps non-uniformly
                u = compute_density_for_timestep_sampling(
                    weighting_scheme=cfg.weighting_scheme,
                    batch_size=bsz,
                    logit_mean=cfg.logit_mean,
                    logit_std=cfg.logit_std,
                    mode_scale=cfg.mode_scale,
                )
                indices = (u * noise_scheduler.config.num_train_timesteps).long()
                timesteps = noise_scheduler.timesteps[indices].to(device=pixel_latents.device)

                # Add noise according to flow matching.
                sigmas = get_sigmas(timesteps, n_dim=pixel_latents.ndim, dtype=pixel_latents.dtype)
                noisy_model_input = (1.0 - sigmas) * pixel_latents + sigmas * noise

                cloth_image_embeds = batch["cloth_image_vit"].to(dtype=weight_dtype)
                
                vton_image_latent = vae.encode(batch["masked_vton_image"].to(dtype=weight_dtype)).latent_dist.sample()
                vton_model_input = (vton_image_latent - vae.config.shift_factor) * vae.config.scaling_factor
                
                cloth_image_latents = vae.encode(batch["cloth_image"].to(dtype=weight_dtype)).latent_dist.sample()
                garm_model_input = (cloth_image_latents - vae.config.shift_factor) * vae.config.scaling_factor
                garm_model_input = garm_model_input if random.random() < cfg.proportion_empty_prompts else torch.zeros_like(garm_model_input)

                _, ref_key, ref_value = transformer_garm(
                    hidden_states=garm_model_input,
                    timestep=timesteps * 0,
                    pooled_projections=cloth_image_embeds,
                    encoder_hidden_states=None,
                    return_dict=False
                )
                
                noise_pred = net(
                    hidden_states=torch.cat([noisy_model_input, vton_model_input, batch["mask_input"].to(dtype=weight_dtype)], dim=1),
                    timesteps=timesteps,
                    pooled_projections=cloth_image_embeds,
                    pose_image=batch["pose_image"].to(dtype=weight_dtype),
                    ref_key=ref_key,
                    ref_value=ref_value,
                    encoder_hidden_states=None,
                    return_dict=False,
                )
                # mask torch.Size([1, 1, 128, 96])
                x0_pred = (noisy_model_input - sigmas * noise_pred) # / (1.0 - sigmas)
                x0_pred = (x0_pred / vae.config.scaling_factor) + vae.config.shift_factor

                pixel_x0_pred = vae.decode(x0_pred, return_dict=False)[0]
                mask = F.interpolate(batch["mask_input"], (pixel_values.shape[2], pixel_values.shape[3]))[0]
                pixel_x0_pred = pixel_x0_pred.float() * mask
                pixel_values = pixel_values.float() * mask
                pixel_x0_pred = torch.fft.fft2(torch.mean(pixel_x0_pred, dim=1))
                pixel_values = torch.fft.fft2(torch.mean(pixel_values, dim=1)) 
                # Follow: Section 5 of https://arxiv.org/abs/2206.00364.
                # Preconditioning of the model outputs.
                # if args.precondition_outputs:
                #     model_pred = model_pred * (-sigmas) + noisy_model_input
                # these weighting schemes use a uniform timestep sampling
                # and instead post-weight the loss
                weighting = compute_loss_weighting_for_sd3(weighting_scheme=args.weighting_scheme, sigmas=sigmas)
                
                loss = torch.mean(
                    (weighting.float() * (pixel_x0_pred.float() - pixel_values.float()) ** 2).reshape(bsz, -1),
                    1,
                )
                loss = loss.mean()
                # loss = F.mse_loss(noise_pred.float(), (noise - pixel_latents).float(), reduction="mean")
                # loss = F.mse_loss(pixel_x0_pred * batch["mask_input"], pixel_values * batch["mask_input"], reduction="mean")
                # Gather the losses across all processes for logging (if we use distributed training).
                avg_loss = accelerator.gather(loss.repeat(cfg.data.train_batch_size)).mean()
                train_loss += avg_loss.item()
                accelerator.backward(loss)
                # Check if the gradient of each model parameter contains NaN
                for name, param in net.named_parameters():
                    if param.grad is not None and torch.isnan(param.grad).any():
                        logger.error(f"Gradient for {name} contains NaN!")

                if accelerator.sync_gradients:
                    params_to_clip = net.parameters()
                    accelerator.clip_grad_norm_(params_to_clip, cfg.max_grad_norm)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad(set_to_none=cfg.set_grads_to_none)

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1
                accelerator.log({"train_loss": train_loss}, step=global_step)
                train_loss = 0.0

                # DeepSpeed requires saving weights on every device; saving weights only on the main process would cause issues.
                if accelerator.distributed_type == DistributedType.DEEPSPEED or accelerator.is_main_process:
                    if global_step % cfg.checkpointing_steps == 0:
                        # _before_ saving state, check if this save would set us over the `checkpoints_total_limit`
                        if cfg.checkpoints_total_limit is not None:
                            checkpoints = os.listdir(os.path.join(cfg.output_dir, "checkpoints"))
                            checkpoints = [d for d in checkpoints if d.startswith("checkpoint")]
                            checkpoints = sorted(checkpoints, key=lambda x: int(x.split("-")[1]))

                            # before we save the new checkpoint, we need to have at _most_ `checkpoints_total_limit - 1` checkpoints
                            if len(checkpoints) >= cfg.checkpoints_total_limit:
                                num_to_remove = len(checkpoints) - cfg.checkpoints_total_limit + 1
                                removing_checkpoints = checkpoints[0:num_to_remove]

                                logger.info(
                                    f"{len(checkpoints)} checkpoints already exist, removing {len(removing_checkpoints)} checkpoints"
                                )
                                logger.info(f"removing checkpoints: {', '.join(removing_checkpoints)}")

                                for removing_checkpoint in removing_checkpoints:
                                    removing_checkpoint = os.path.join(cfg.output_dir, "checkpoints", removing_checkpoint)
                                    shutil.rmtree(removing_checkpoint)

                        save_path = os.path.join(cfg.output_dir, "checkpoints", f"checkpoint-{global_step}")
                        accelerator.save_state(save_path)
                        logger.info(f"Saved state to {save_path}")
                        
                        unwarp_net = accelerator.unwrap_model(net)
                        pipeline = StableDiffusion3TryOnPipeline.from_pretrained(cfg.pretrained_model_name_path, transformer_vton=unwarp_net.transformer_vton, pose_guider=unwarp_net.pose_guider)
                        pipeline.save_pretrained(cfg.output_dir)
                        # state_dict = {
                        #     "pose_guider": unwarp_net.pose_guider.state_dict(),
                        #     "transformer_vton": unwarp_net.transformer_vton.state_dict(),
                        # }
                        # save_models(state_dict, cfg.output_dir, global_step)

            logs = {"loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)
            accelerator.log(logs, step=global_step)

            if global_step >= cfg.max_train_steps:
                break
    # Create the pipeline using using the trained modules and save it.
    accelerator.wait_for_everyone()
    accelerator.end_training()


def save_models(state_dict, save_dir, global_step):
    pose_guider_save_path = os.path.join(save_dir, f"pose_guider-{global_step}.pt")
    transformer_vton_save_path = os.path.join(save_dir, f"transformer_vton-{global_step}.pt")
    
    os.makedirs(os.path.dirname(pose_guider_save_path), exist_ok=True)  
    os.makedirs(os.path.dirname(transformer_vton_save_path), exist_ok=True)  
    
    torch.save(state_dict['pose_guider'], pose_guider_save_path)
    torch.save(state_dict['transformer_vton'], transformer_vton_save_path)

def _pack_latents(latents, batch_size, num_channels_latents, height, width):
    latents = latents.view(batch_size, num_channels_latents, height // 2, 2, width // 2, 2)
    latents = latents.permute(0, 2, 4, 1, 3, 5)
    latents = latents.reshape(batch_size, (height // 2) * (width // 2), num_channels_latents * 4)

    return latents
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="./configs/train.yaml")
    args = parser.parse_args()

    if args.config[-5:] == ".yaml":
        config = OmegaConf.load(args.config)
    else:
        raise ValueError("Do not support this format config file")
    main(config)

