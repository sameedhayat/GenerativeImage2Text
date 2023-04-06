from common import Config
import json
import os.path as op
from common import qd_tqdm as tqdm
from common import json_dump
from common import pilimg_from_base64
from torch_common import recursive_to_device
from tsv_io import TSVFile, tsv_writer, tsv_reader
from common import write_to_file
import torch
import PIL
from pprint import pformat
import logging
from transformers import BertTokenizer
import torchvision.transforms as transforms
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize
from PIL import Image
from azfuse import File

from common import init_logging
from common import parse_general_args
from tsv_io import load_from_yaml_file
from torch_common import torch_load
from torch_common import load_state_dict
from torch_common import resize_2d_pos_embed
from layers.CLIP import clip
from layers.decoder import (TransformerDecoderTextualHead,
                             AutoRegressiveBeamSearch, GeneratorWithBeamSearch)
from layers.decoder import CaptioningModel
from process_image import load_image_by_pil
from data_layer.transform import RenameKey, SelectTransform
from data_layer.transform import ImageTransform2Dict
from data_layer.transform import get_inception_train_transform
from data_layer.builder import collate_fn
from model import get_git_model

import os

def get_files_path(directory, extension):
    file_paths = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(extension):
                file_path = os.path.join(root, file)
                file_paths.append(file_path)
    return file_paths

def get_data(image_file, prefix, target, tokenizer, image_transform):
    max_text_len = 40
    prefix_encoding = tokenizer(
        prefix, padding='do_not_pad',
        add_special_tokens=False,
        truncation=True, max_length=max_text_len)
    target_encoding = tokenizer(
        target, padding='do_not_pad',
        add_special_tokens=False,
        truncation=True, max_length=max_text_len)
    need_predict = [0] * len(prefix_encoding['input_ids']) + [1] * len(target_encoding['input_ids'])
    payload = prefix_encoding['input_ids'] + target_encoding['input_ids']
    if len(payload) > max_text_len:
        payload = payload[-(max_text_len - 2):]
        need_predict = need_predict[-(max_text_len - 2):]
    input_ids = [tokenizer.cls_token_id] + payload + [tokenizer.sep_token_id]
    need_predict = [0] + need_predict + [1]

    im = load_image_by_pil(image_file)

    data = {
        'caption_tokens': torch.tensor(input_ids),
        #'caption_lengths': len(input_ids),
        'need_predict': torch.tensor(need_predict),
        'image': im,
        # 'rect' field can be fed in 'caption', which tells the bounding box
        # region of the image that is described by the caption. In this case,
        # we can optionally crop the region.
        'caption': {},
        # this iteration can be used for crop-size selection so that all GPUs
        # can process the image with the same input size
        'iteration': 0,
    }
    data = image_transform(data)

    return data

def get_image_transform(cfg):
    return get_multi_scale_image_transform(cfg, is_train=True)

def get_default_mean():
    return [0.485, 0.456, 0.406]

def get_default_std():
    return [0.229, 0.224, 0.225]

def get_transform_image_norm(cfg, default=None):
    if cfg.data_normalize == 'default':
        normalize = transforms.Normalize(
            mean=get_default_mean(), std=get_default_std())
    elif cfg.data_normalize == 'clip':
        # clip model
        normalize = transforms.Normalize(
            (0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711))
    else:
        raise NotImplementedError(cfg.data_normalize)
    return normalize

def get_transform_vit_default(cfg, is_train):
    default_normalize = transforms.Normalize(
            mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
    normalize = get_transform_image_norm(cfg, default_normalize)
    transform = get_inception_train_transform(
        bgr2rgb=True,
        crop_size=cfg.train_crop_size,
        normalize=normalize,
        small_scale=cfg.input_small_scale,
        no_color_jitter=cfg.no_color_jitter,
        no_flip=cfg.no_flip,
        no_aspect_dist=cfg.no_aspect_dist,
        resize_crop=cfg.resize_crop,
        max_size=cfg.train_max_size,
        interpolation=cfg.interpolation or Image.BILINEAR,
    )
    return transform

def get_transform_image(cfg, is_train):
    train_transform = cfg.train_transform
    if train_transform == 'vitp':
        transform = get_transform_vit_default(
            cfg, is_train=is_train)
    else:
        raise NotImplementedError(train_transform)
    return transform

class ImageTransform2Images(object):
    def __init__(self, sep_transform, first_joint=None):
        self.image_transform = sep_transform
        self.first_joint = first_joint

    def __call__(self, imgs):
        if self.first_joint is not None:
            imgs = self.first_joint(imgs)
        return [self.image_transform(im) for im in imgs]

    def __repr__(self):
        return 'ImageTransform2Images(image_transform={})'.format(
            self.image_transform,
        )

def get_transform_images(cfg, is_train):
    trans = get_transform_image(cfg, is_train)
    trans = ImageTransform2Images(trans)
    return trans

def trans_select_for_crop_size(
    data, train_crop_sizes,
    iteration_multi=0,
):
    if iteration_multi <= 0:
        if len(train_crop_sizes) == 1:
            idx = 0
        else:
            idx = data['iteration'] % len(train_crop_sizes)
    elif data['iteration'] <= iteration_multi:
        idx = data['iteration'] % len(train_crop_sizes)
    else:
        idx = -1
    return idx

def get_multi_scale_image_transform(cfg, is_train, get_one=get_transform_image):
    def get_multi_res_transform(s):
        old = cfg.train_crop_size if is_train else cfg.test_crop_size
        all_t = []
        multi_res_factors = cfg.multi_res_factors or []
        for i, f in enumerate(multi_res_factors):
            if is_train:
                cfg.train_crop_size = s // f
            else:
                cfg.test_crop_size = s // f
            key = 'image_{}'.format(i)
            all_t.append(RenameKey({'image': key}, not_delete_origin=True))
            t = get_one(cfg, is_train)
            t = ImageTransform2Dict(t, key=key)
            all_t.append(t)
        # get_one depends on train_crop_size
        if is_train:
            cfg.train_crop_size = s
        else:
            cfg.test_crop_size = s
        t = get_one(cfg, is_train)
        t = ImageTransform2Dict(t)
        all_t.append(t)
        if is_train:
            cfg.train_crop_size = old
        else:
            cfg.test_crop_size = old
        return transforms.Compose(all_t)

    if is_train:
        if cfg.min_size_range32 is None:
            train_crop_sizes = [cfg.train_crop_size]
        else:
            train_crop_sizes = list(range(
                cfg.min_size_range32[0],
                cfg.min_size_range32[1] + cfg.patch_size - 1, cfg.patch_size,
            ))
    else:
        train_crop_sizes = [cfg.test_crop_size]

    crop_trans = []
    for s in train_crop_sizes:
        t = get_multi_res_transform(s)
        crop_trans.append(t)
    iteration_multi = 0
    image_transform = SelectTransform(
        crop_trans,
        lambda d: trans_select_for_crop_size(
            d, train_crop_sizes, iteration_multi))
    return image_transform

def forward_backward_example(image_files, captions, prefixs=None):
    if prefixs is None:
        prefixs = [''] * len(captions)
    cfg = {
        'crop_region_extend_in_datatransform': 4,
        'data_normalize': 'clip',
        'train_crop_size': 224,
        'input_small_scale': 0.8,
        'no_color_jitter': True,
        'no_flip': True,
        'no_aspect_dist': True,
        'interpolation': 'bicubic',
        'min_size_range32': [160, 224], # in pretraining, it is multi-scale from 160 to 224; while for fine-tuning, it is single scale
        'patch_size': 16,
        'train_transform': 'vitp',
    }
    cfg = Config(cfg, {})
    all_data = []
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)
    image_transform = get_image_transform(cfg)
    for image_file, prefix, target in zip(image_files, prefixs, captions):
        data = get_data(image_file, prefix, target,
                        tokenizer, image_transform)
        all_data.append(data)
    data = collate_fn(all_data)
    logging.info(image_transform)
    data = recursive_to_device(data, 'cuda')

    param = {}
    model = get_git_model(tokenizer, param)
    model.train()
    model.cuda()
    loss_dict = model(data)
    loss = sum(loss_dict.values())
    loss.backward()
    logging.info(loss)


import os
import csv
import json

def get_file_contents(directory, file_extension):
    """
    Returns a list of the contents of all files with the given extension in the given directory
    and its subdirectories. Currently supports CSV and JSON files only.
    
    :param directory: The directory to search for files.
    :param file_extension: The file extension to search for (e.g. 'csv' or 'json').
    :return: A list of the contents of all files with the given extension in the directory.
    """
    file_contents = []
    
    # Open the CSV file and read it as a string
    with open(directory, 'r') as f:
      csv_string = f.read()

    return csv_string


import torch
import deepspeed
from transformers import BertTokenizer
from deepspeed.ops.adam import FusedAdam

def train_deepspeed(image_dir, caption_dir, prefixs=None, batch_size=16, num_epochs=10, model_save_path='model.pt'):
    

    image_files = get_files_path(image_dir, 'png')
    caption_files = get_files_path(caption_dir, 'csv')

    if prefixs is None:
        prefixs = [''] * len(caption_files)
    cfg = {
        'crop_region_extend_in_datatransform': 4,
        'data_normalize': 'clip',
        'train_crop_size': 224,
        'input_small_scale': 0.8,
        'no_color_jitter': True,
        'no_flip': True,
        'no_aspect_dist': True,
        'interpolation': 'bicubic',
        'min_size_range32': [160, 224],
        'patch_size': 16,
        'train_transform': 'vitp',
    }
    cfg = Config(cfg, {})
    all_data = []
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)
    image_transform = get_image_transform(cfg)
    for image_file, prefix, target in zip(image_files, prefixs, caption_files):
        target = get_file_contents(caption_files)
        data = get_data(image_file, prefix, target,
                        tokenizer, image_transform)
        all_data.append(data)
    data = collate_fn(all_data)
    logging.info(image_transform)
    data = recursive_to_device(data, 'cuda')

    param = {}
    model = get_git_model(tokenizer, param)
    model.train()
    model.cuda()

    # Wrap the model with DeepSpeed
    model_engine, _, _ = deepspeed.initialize(model=model, model_parameters=param, training_data=data)

    optimizer = FusedAdam(model.parameters(), lr=1e-4)

    best_loss = float('inf')

    for epoch in range(num_epochs):
        model_engine.train()
        total_loss = 0.0
        num_batches = 0
        for batch in model_engine.backward_dataloader(data, batch_size=batch_size):
            model_engine.zero_grad()
            loss_dict = model_engine(batch)
            loss = sum(loss_dict.values())
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
            num_batches += 1

        avg_loss = total_loss / num_batches
        logging.info(f'Epoch {epoch + 1}/{num_epochs} - Avg Loss: {avg_loss:.4f}')

        if (epoch + 1) % 5 == 0:
            torch.save(model.state_dict(), f'{model_save_path}_{epoch + 1}.pt')

        if avg_loss < best_loss:
            best_loss = avg_loss
            torch.save(model.state_dict(), f'best_{model_save_path}')

    logging.info('Training complete.')


import os
import csv
from PIL import Image
import torch
from torch.utils.data import Dataset, DataLoader

class ImageDataset(Dataset):
    def __init__(self, image_files, prefixes, csv_files):
        self.image_files = image_files
        self.prefixes = prefixes
        self.csv_files = csv_files
        
    def __getitem__(self, index):
        # Load image
        image_file = self.image_files[index]

        # Load corresponding CSV file
        csv_file = os.path.splitext(image_file)[0] + '.csv'
        csv_path = os.path.join(self.csv_dir, csv_file)
        with open(csv_path, 'r') as f:
            csv_reader = csv.reader(f)
            # You can parse the CSV data and use it as needed
            # Here, we just read the first row as a list
            csv_data = next(csv_reader)

        return image, csv_data

    def __len__(self):
        return len(self.image_files)

from torch.utils.data import Dataset
from torchvision.transforms import ToTensor

class MyDataset(Dataset):
    def __init__(self, image_files, prefixs, caption_files, tokenizer, image_transform):
        self.image_files = image_files
        self.prefixs = prefixs
        self.caption_files = caption_files
        self.tokenizer = tokenizer
        self.image_transform = image_transform
        

    def __getitem__(self, index):
        image_file = self.image_files[index]
        prefix = self.prefixs[index]
        target = self.caption_files[index]
        
        target = get_file_contents(target, "csv") # Implement your logic to read caption file
        data = get_data(image_file, prefix, target, self.tokenizer, self.image_transform) # Implement your logic to get data
        return data

    def __len__(self):
        return len(self.image_files)


def train_deepspeed_lazy(image_dir, caption_dir, deepspeed_args, prefixs=None, batch_size=16, num_epochs=10, model_save_path='model.pt'):
    print(deepspeed_args)
    image_files = get_files_path(image_dir, 'png')
    caption_files = get_files_path(caption_dir, 'csv')

    if prefixs is None:
        prefixs = [''] * len(caption_files)
    cfg = {
        'crop_region_extend_in_datatransform': 4,
        'data_normalize': 'clip',
        'train_crop_size': 224,
        'input_small_scale': 0.8,
        'no_color_jitter': True,
        'no_flip': True,
        'no_aspect_dist': True,
        'interpolation': 'bicubic',
        'min_size_range32': [160, 224],
        'patch_size': 16,
        'train_transform': 'vitp',
    }
    cfg = Config(cfg, {})
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)
    image_transform = get_image_transform(cfg)

    data = MyDataset(image_files, prefixs, caption_files, tokenizer, image_transform)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # Wrap the data generator with DataLoader
    data_loader = torch.utils.data.DataLoader(data, batch_size=batch_size, collate_fn=collate_fn)
    # Move the DataLoader to CUDA
    dataloader = dataloader.to('cuda')

    param = {}
    model = get_git_model(tokenizer, param)
    
    model.train()
    model.cuda()
    
    
    # Wrap the model with DeepSpeed
    model_engine, optimizer, _, _  = deepspeed.initialize(model=model, model_parameters=model.parameters(), args=deepspeed_args)

    #optimizer = FusedAdam(model.parameters(), lr=1e-4)

    best_loss = float('inf')

    for epoch in range(num_epochs):
        model_engine.train()
        total_loss = 0.0
        num_batches = 0
        for step, batch in enumerate(data_loader):
            loss = model_engine(batch)

            #runs backpropagation
            model_engine.backward(loss)

            #weight update
            model_engine.step()
            #model_engine.zero_grad()
            #loss_dict = model_engine(batch)
            loss = sum(loss.values())
            #loss.backward()
            #optimizer.step()
            total_loss += loss.item()
            num_batches += 1

        avg_loss = total_loss / num_batches
        logging.info(f'Epoch {epoch + 1}/{num_epochs} - Avg Loss: {avg_loss:.4f}')

        if (epoch + 1) % 5 == 0:
            torch.save(model.state_dict(), f'{model_save_path}_{epoch + 1}.pt')

        if avg_loss < best_loss:
            best_loss = avg_loss
            torch.save(model.state_dict(), f'best_{model_save_path}')

    logging.info('Training complete.')


def speed_test_forward_backward():
    duplicate = 32
    image_files = ['aux_data/images/1.jpg', 'aux_data/images/2.jpg'] * duplicate
    captions = ['a couple of boats in a large body of water.', 'a view of a mountain with a tree'] * duplicate

    prefixs = [''] * len(captions)
    cfg = {
        'crop_region_extend_in_datatransform': 4,
        'data_normalize': 'clip',
        'train_crop_size': 224,
        'input_small_scale': 0.8,
        'no_color_jitter': True,
        'no_flip': True,
        'no_aspect_dist': True,
        'interpolation': 'bicubic',
        'min_size_range32': [160, 224], # in pretraining, it is multi-scale from 160 to 224; while for fine-tuning, it is single scale
        'patch_size': 16,
        'train_transform': 'vitp',
    }
    cfg = Config(cfg, {})
    all_data = []
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)
    image_transform = get_image_transform(cfg)
    for image_file, prefix, target in zip(image_files, prefixs, captions):
        data = get_data(image_file, prefix, target,
                        tokenizer, image_transform)
        all_data.append(data)
    data = collate_fn(all_data)
    logging.info(image_transform)
    data = recursive_to_device(data, 'cuda')
    data['image'] = data['image'].to(torch.float16)

    param = {}
    model = get_git_model(tokenizer, param)
    model.train()
    model.cuda()
    model.half()

    # warmup
    for _ in range(2):
        loss_dict = model(data)
        loss = sum(loss_dict.values())
        loss.backward()

    import time
    start = time.time()
    for iteration in range(1000):
        loss_dict = model(data)
        loss = sum(loss_dict.values())
        loss.backward()
        if (iteration % 10) == 0:
            end = time.time()
            speed = data['image'].shape[0] * 100 / (end - start)
            if iteration > 0:
                logging.info('speed = {}'.format(speed))
            start = time.time()

    logging.info(loss)


if __name__ == '__main__':
    init_logging()
    kwargs = parse_general_args()
    logging.info('param:\n{}'.format(pformat(kwargs)))
    function_name = kwargs['type']
    del kwargs['type']
    print(kwargs)
    locals()[function_name](**kwargs)

