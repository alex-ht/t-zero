#!/usr/bin/env python
# coding=utf-8
# Copyright BigScience, The HuggingFace Team and The HuggingFace Inc. team. All rights reserved.
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
# limitations under the License.
"""
Reproduce the main evaluation in `Multitask Prompted Training Enables Zero-Shot Task Generalization` using PyTorch.

This script is heavily adapted from https://github.com/huggingface/transformers/blob/7533d30acd975027e83a548e4c38e06fa335291b/examples/pytorch/multiple-choice/run_swag_no_trainer.py
"""

import argparse
import logging
import os
import random
import json

import datasets
import torch
from datasets import load_dataset
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import transformers
from accelerate import Accelerator
from transformers import (
    AutoConfig,
    AutoTokenizer,
    default_data_collator,
)
import evaluate
from promptsource.templates import DatasetTemplates

from t0.data_collator import DataCollatorForMultipleChoice
from t0.model import ModelBase

logger = logging.getLogger(__name__)

STORY_CLOZE_DIR = "/gpfswork/rech/six/commun/code/tr13f-6B3-ml-t0/story_cloze_data"
XSTORY_CLOZE_DIR = "/gpfswork/rech/six/commun/code/tr13f-6B3-ml-t0/xstory_cloze_data"

def parse_args():
    parser = argparse.ArgumentParser(description="Reproduce main evaluation in T0.")
    parser.add_argument(
        "--dataset_name",
        type=str,
        help="The name of the dataset to use (via the datasets library).",
        required=True,
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The configuration name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--template_config_name",
        type=str,
        default=None,
        help="The name of the dataset_config_name of the template we want to use, example: use XNLI En prompts for XNLI Fr",
    )
    parser.add_argument(
        "--template_name",
        type=str,
        default=None,
        help="The template/prompt name. If None, we run all templates.",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="validation",
        help="The dataset split, e.g. train.",
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=1024,
        help=(
            "The maximum total input sequence length after tokenization. Sequences longer than this will be truncated,"
            " sequences shorter will be padded if `--pad_to_max_lengh` is passed."
        ),
    )
    parser.add_argument(
        "--target_max_length",
        type=int,
        default=256,
        help="Target max length. Sequences longer than this will be truncated."
    )
    parser.add_argument(
        "--pad_to_max_length",
        action="store_true",
        help="If passed, pad all samples to `max_length`. Otherwise, dynamic padding is used.",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models. The list of T0 variants can be found on `https://huggingface.co/bigscience/T0_3B`",
        required=True,
    )
    parser.add_argument(
        "--config_name",
        type=str,
        default=None,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        type=str,
        default=None,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=None,
        help="Where to store the final model."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Activate debug mode and run training only with a subset of data.",
    )
    parser.add_argument(
        "--prefixlm",
        action="store_true",
        help="Use prefix language model.",
    )
    parser.add_argument(
        "--dtype",
        type=str,
        default=None,
        help="torch_dtype of the model, e.g. bfloat16"
    )
    parser.add_argument(
        "--nospace",
        action="store_true",
        help="Do not prepend a space to targets.",
    )

    args = parser.parse_args()

    # TODO @thomasw21 hack!
    if args.dataset_config_name == "None":
        args.dataset_config_name = None
    if args.template_config_name == "None":
        args.template_config_name = None

    return args

def run_template(template_name, prompts, model, tokenizer, raw_datasets, accelerator: Accelerator, args):

    # Handle the output directory creation
    result_dir = None
    if args.output_dir is not None and accelerator.is_main_process:
        paths = [
            args.dataset_name,
            args.dataset_config_name,
            template_name,
        ]
        result_dir = os.path.join(
            args.output_dir,
            *[path.replace(" ", "_").replace("/", "_") for path in paths if path is not None]
        )
        os.makedirs(result_dir, exist_ok=True)

        if os.path.exists(os.path.join(result_dir, "results.json")):
            accelerator.print(f"Skipping as result file exists.")           
            return

    # This copa template gets split due to the comma in the slurm script
    if template_name == "C1 or C2? premise":
        template_name = "C1 or C2? premise, so/because…"
    template = prompts[template_name]


    # Preprocessing the datasets.
    # First we tokenize all the texts.
    padding = "max_length" if args.pad_to_max_length else False
    column_names = raw_datasets.column_names
    def preprocess_function(examples):
        bs = len(examples[column_names[0]])
        # List of inputs ; [bs]
        input_texts = []
        # List of targets ; [bs]
        target_texts = []
        # List of List of answer choices ; [bs, x]
        answer_choices_texts = []
        for i in range(bs):
            ex = {
                k: examples[k][i]
                for k in column_names
            }
            applied = template.apply(ex)
            assert len(applied) == 2, f"Incompatible template: {template_name}"
            input, target = applied

            if isinstance(target, list):
                assert len(target) == 1, f"Got multiple targets: {target}"
                target = target[0]
            if not args.nospace:
                target = " " + target
            ex_answer_choices = template.get_answer_choices_list(ex)
            # Nostrip solution
            #target_strip = len(target) - len(target.strip())
            #ex_answer_choices = [target[:target_strip] + c for c in ex_answer_choices]
            if not args.nospace:
                ex_answer_choices = [" " + c for c in ex_answer_choices]
            assert target in ex_answer_choices, f"Expected {target} in {ex_answer_choices}"
            input_texts.append(input)
            target_texts.append(target)
            answer_choices_texts.append(ex_answer_choices)
        # "input_ids": [bs[max_length]]
        tokenized_inputs = tokenizer(
            input_texts,
            padding=padding,
            max_length=args.max_length,
            truncation=True,
            add_special_tokens=False,
        )
        # List of answer choices
        # "input_ids": [bs[X[max_length]]], where max_length is a possibly padded answer choice
        tokenized_targets = [
            tokenizer(
                ans_choi,
                # padding is on the right here.
                padding=False,
                max_length=args.max_length,
                truncation=True,
            )
            for ans_choi in answer_choices_texts
        ]

        # Duplicate input for each answer choice, where X are the answer choices
        # "input_ids": [bs[X[max_length]]], where max_length is a possibly padded input text
        features = {
            k: [
                [elem for _ in range(len(tokenized_targets[idx]["input_ids"]))]
                for idx, elem in enumerate(v)
            ]
            for k, v in tokenized_inputs.items()
        }
        # Get the corrext answer choice [bs[X[max_length]]]
        # This should be the same as features["labels"] = tokenized_targets
        features["labels"] = [
            tokenized_targets[idx]["input_ids"]
            for idx in range(bs)
        ]
        features["labels_attention_mask"] = [
            tokenized_targets[idx]["attention_mask"]
            for idx in range(bs)
        ]
        # Indices of correct targets [bs[1]]
        features["targets"] = [
            answer_choices_texts[idx].index(t)
            for idx, t in enumerate(target_texts)
        ]

        return features

    with accelerator.main_process_first():
        eval_dataset = raw_datasets.map(
            preprocess_function, batched=True, remove_columns=column_names
        )

    # Log a few random samples from the eval set:
    for index in random.sample(range(len(eval_dataset)), 3):
        logger.info(f"Sample {index} of the training set: {eval_dataset[index]}.")

    # DataLoaders creation:
    if args.pad_to_max_length:
        # If padding was already done ot max length, we use the default data collator that will just convert everything
        # to tensors.
        data_collator = default_data_collator
    else:
        # Otherwise, `DataCollatorWithPadding` will apply dynamic padding for us (by padding to the maximum length of
        # the samples passed). When using mixed precision, we add `pad_to_multiple_of=8` to pad all tensors to multiple
        # of 8s, which will enable the use of Tensor Cores on NVIDIA hardware with compute capability >= 7.5 (Volta).
        data_collator = DataCollatorForMultipleChoice(
            tokenizer, pad_to_multiple_of=(8 if accelerator.use_fp16 else None)
        )

    eval_dataloader = DataLoader(eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size)

    # Prepare everything with our `accelerator`.
    model, eval_dataloader = accelerator.prepare(model, eval_dataloader)

    # Metrics
    metric = evaluate.load(
        "accuracy",
        process_id=accelerator.process_index,
        num_process=accelerator.num_processes,
        experiment_id=f"{args.dataset_name}_{args.dataset_config_name}_{args.template_name}".replace('/', '_').replace(' ', '_')
    )

    # Eval!
    total_batch_size = args.per_device_eval_batch_size * accelerator.num_processes

    logger.info("***** Running evaluation *****")
    logger.info(f"  Num examples = {len(eval_dataset)}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_eval_batch_size}")
    logger.info(f"  Total eval batch size (w. parallel, distributed) = {total_batch_size}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(len(eval_dataloader)), disable=not accelerator.is_local_main_process)

    model.eval()
    
    for batch in eval_dataloader:
        with torch.no_grad():
            predictions = model(batch, prefixlm=args.prefixlm)

        metric.add_batch(
            predictions=accelerator.gather_for_metrics(predictions),
            references=accelerator.gather_for_metrics(batch["targets"])
        )

        progress_bar.update(1)

    eval_metric = metric.compute()
    accelerator.print(f"Result: {eval_metric}")

    results = {
        "dataset_name": args.dataset_name,
        "dataset_config_name": args.dataset_config_name,
        "template_name": template_name,
        "evaluation": eval_metric,
        "arguments": str(args)
    }
    if accelerator.is_main_process:
        if result_dir is not None:
            with open(os.path.join(result_dir, "results.json"), "w") as f:
                json.dump(results, f, indent=2)

def main():
    args = parse_args()

    # Initialize the accelerator. We will let the accelerator handle device placement for us.
    accelerator = Accelerator()
    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state)

    # Setup logging, we only want one process per machine to log things on the screen.
    # accelerator.is_local_main_process is only True for one process per machine.
    logger.setLevel(logging.INFO if accelerator.is_local_main_process else logging.ERROR)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    accelerator.wait_for_everyone()

    # In distributed evaluation, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    # Downloading and loading a dataset from the hub.
    if args.dataset_name.lower() == "story_cloze".lower():   
        raw_datasets = load_dataset(args.dataset_name, args.dataset_config_name, split="validation", data_dir=STORY_CLOZE_DIR)
    elif "xstory_cloze".lower() in args.dataset_name.lower():
        raw_datasets = load_dataset(args.dataset_name, args.dataset_config_name, split="validation", data_dir=XSTORY_CLOZE_DIR)
    # Parsing problems with this template name
    elif (args.template_name is not None) and (args.template_name.startswith("C1 or C2? premise")):
        raw_datasets = load_dataset(args.dataset_name, args.dataset_config_name, split="validation")
    elif args.dataset_name.lower() == "anli":
        raw_datasets = load_dataset(args.dataset_name, None, split=args.split)
    else:
        raw_datasets = load_dataset(args.dataset_name, args.dataset_config_name, split=args.split)

    # Trim a number of evaluation examples
    if args.debug:
        raw_datasets = raw_datasets.select(range(min(len(raw_datasets),100)))

    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    if args.config_name:
        config = AutoConfig.from_pretrained(args.config_name)
    elif args.model_name_or_path:
        config = AutoConfig.from_pretrained(args.model_name_or_path)
    else:
        raise ValueError(
            "Either `args.config_name` or `args.model_name_or_path` should be provided."
        )

    if args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_name, use_fast=not args.use_slow_tokenizer, padding_side="left")
    elif args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, use_fast=not args.use_slow_tokenizer, padding_side="left")
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )

    if tokenizer.pad_token is None:
        for token in [tokenizer.eos_token, tokenizer.bos_token, tokenizer.sep_token]:
            if token is not None:
                tokenizer.pad_token = token
        if tokenizer.pad_token is None:
            raise ValueError("Please define a pad token id.")


    model = ModelBase.from_config(
        config=config,
        model_name_or_path=args.model_name_or_path,
        torch_dtype=getattr(torch, args.dtype) if args.dtype is not None else None,
    )
    # Let accelerate handle the dtype
    if args.dtype is None:
        model = accelerator.prepare_model(model)

    # Get the prompt to apply and the possible targets.
    # TODO(Victor): If pulling from pre-processed data, remove this logic.

    if (args.dataset_config_name is None and args.template_config_name is None) or args.dataset_name == "anli":
        prompt_dataset_name = f"{args.dataset_name}"
    elif args.template_config_name is not None:
        prompt_dataset_name = f"{args.dataset_name}/{args.template_config_name}"
    else:
        prompt_dataset_name = f"{args.dataset_name}/{args.dataset_config_name}"

    prompts = DatasetTemplates(
        prompt_dataset_name
    )
    
    run_template(
        template_name=args.template_name,
        prompts=prompts,
        model=model,
        tokenizer=tokenizer,
        raw_datasets=raw_datasets,
        accelerator=accelerator,
        args=args
    )

if __name__ == "__main__":
    main()
