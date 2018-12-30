# from config import cfg
# cfg.merge_from_file('configs/e2e_deformconv_mask_rcnn_R_50_C5_1x.yaml')
# cfg.freeze()

from modeling.detectors.deconv_rcnn import DeformConvRCNN
from modeling.detectors.predictor import Predictor
from engine import inference
from data.build import make_data_loader
from solver.build import make_optimizer_DeConv as make_optimizer

import datetime
def get_time():
    return (str(datetime.datetime.now())[:-10]).replace(' ','-').replace(':','-')
import logging
import time
import os
from tensorboardX import SummaryWriter
import torch
import torch.distributed as dist
from utils.comm import get_world_size
from utils.metric_logger import MetricLogger
from utils.miscellaneous import mkdir
from tqdm import tqdm
from pathlib import Path
import cv2

class Learner(object):
    def __init__(self, cfg):
        self.cfg = cfg.clone()
        self.device = torch.device(cfg.MODEL.DEVICE)
        self.model = DeformConvRCNN(cfg).to(self.device)
        [*self.model.backbone.modules()][1].stem.load_state_dict(torch.load(cfg.MODEL.BACKBONE.PRETRAINED_STEM_WEIGHTS))
        [*self.model.backbone.modules()][1].layer1.load_state_dict(torch.load(cfg.MODEL.BACKBONE.PRETRAINED_LAYER1_WEIGHTS))
        self.train_loader = make_data_loader(cfg, is_train=True)
        self.val_loader = make_data_loader(cfg, is_train=False)[0]
        self.optimizer = make_optimizer(cfg, self.model)
        self.logger = logging.getLogger("Deform_Conv_RCNN.trainer")
        self.writer = SummaryWriter(cfg.WRITER_DIR)
        self.predictor = Predictor(cfg, self.model, 
                                   confidence_threshold=cfg.TEST.CONF_THRES, 
                                   min_image_size=cfg.TEST.MIN_IMG_SIZE)
        self.step = 0
        self.milestones = cfg.SOLVER.STEPS
        self.workspace = Path(cfg.WORKSPACE)
        self.board_loss_every = len(self.train_loader.dataset) // cfg.SOLVER.IMS_PER_BATCH // 100
        self.evaluate_every = len(self.train_loader.dataset) // cfg.SOLVER.IMS_PER_BATCH // 4
        self.save_every = len(self.train_loader.dataset) // cfg.SOLVER.IMS_PER_BATCH // 5
        self.board_pred_image_every = len(self.train_loader.dataset) // cfg.SOLVER.IMS_PER_BATCH // 5
        self.inference_every = len(self.train_loader.dataset) // cfg.SOLVER.IMS_PER_BATCH // 1
        
        # test only
        self.board_loss_every = 10
        self.evaluate_every = 10
        self.save_every = 10
        self.board_pred_image_every = 10
        self.inference_every = 10
        # test only
    
    def schedule_lr(self):
        if self.step in self.milestones:
            print('lr scheduled when meeting {} steps'.format(self.step))
            for params in self.optimizer.param_groups:                 
                params['lr'] /= 10
            print(self.optimizer)
    
    def evaluate(self, num=None):  
        running_loss = 0.
        running_loss_classifier = 0.
        running_loss_box_reg = 0.
        running_loss_mask = 0.
        running_loss_objectness = 0.
        running_loss_rpn_box_reg = 0.
        running_loss_mimicking_cls = 0.
        running_loss_mimicking_cos_sim = 0.
        if num == None:
            total_num = len(self.val_loader)
        else:
            assert num <= len(self.val_loader), 'validation batches should be less than total' 
            total_num = num
        with torch.no_grad():
            counts = 0
            for images, targets, _ in tqdm(iter(self.val_loader)):
                images = images.to(self.device)
                targets = [target.to(self.device) for target in targets]
                loss_dict = self.model(images, targets)
                losses = sum(loss for loss in loss_dict.values())
                running_loss += losses.item()
                running_loss_classifier += loss_dict['loss_classifier']
                running_loss_box_reg += loss_dict['loss_box_reg']
                running_loss_mask += loss_dict['loss_mask']
                running_loss_objectness += loss_dict['loss_objectness']
                running_loss_rpn_box_reg += loss_dict['loss_rpn_box_reg']
                running_loss_mimicking_cls += loss_dict['loss_mimicking_cls']
                running_loss_mimicking_cos_sim += loss_dict['loss_mimicking_cos_sim']
                counts += 1
                if counts > total_num:
                    break
        return running_loss / total_num, \
            running_loss_classifier / total_num, \
            running_loss_box_reg / total_num, \
            running_loss_mask / total_num, \
            running_loss_objectness / total_num,\
            running_loss_rpn_box_reg / total_num, \
            running_loss_mimicking_cls / total_num, \
            running_loss_mimicking_cos_sim / total_num
    
    def save_state(self, val_loss, mmap, to_save_folder=False, model_only=False):
        if to_save_folder:
            save_path = self.workspace/'save'
        else:
            save_path = self.workspace/'model' 
        time = get_time()
        torch.save(
            self.model.state_dict(), save_path /
            ('model_{}_val_loss:{}_mmap:{}_step:{}.pth'.format(time,
                                                                val_loss, 
                                                                mmap, 
                                                                self.step)))
        if not model_only:
            torch.save(
                self.optimizer.state_dict(), save_path /
                ('optimizer_{}_val_loss:{}_mmap:{}_step:{}.pth'.format(time,
                                                                        val_loss, 
                                                                        mmap, 
                                                                        self.step)))
    
    def load_state(self, fixed_str, from_save_folder=False, model_only=False):
        if from_save_folder:
            save_path = self.workspace/'save'
        else:
            save_path = self.workspace/'model'           
        self.model.load_state_dict(torch.load(save_path/'model_{}'.format(fixed_str)))
        print('load model_{}'.format(fixed_str))
        if not model_only:
            self.optimizer.load_state_dict(torch.load(save_path/'optimizer_{}'.format(fixed_str)))
            print('load optimizer_{}'.format(fixed_str))
    
    def resume_training_load(self, from_save_folder=False):
        if from_save_folder:
            save_path = self.workspace/'save'
        else:
            save_path = self.workspace/'model'  
        sorted_files = sorted([*save_path.iterdir()],  key=lambda x: os.path.getmtime(x), reverse=True)
        seeking_flag = True
        index = 0
        while seeking_flag:
            if index > len(sorted_files) - 2:
                break
            file_a = sorted_files[index]
            file_b = sorted_files[index + 1]
            if file_a.name.startswith('model'):
                fix_str = file_a.name[6:]
                self.step = int(fix_str.split(':')[-1].split('.')[0]) + 1
                if file_b.name == ''.join(['optimizer', '_', fix_str]):                    
                    self.model.load_state(fix_str, from_save_folder)
                    return
                else:
                    index += 1
                    continue
            elif file_a.name.startswith('optimizer'):
                fix_str = file_a.name[10:]
                self.step = int(fix_str.split(':')[-1].split('.')[0]) + 1
                if file_b.name == ''.join(['model', '_', fix_str]):
                    self.load_state(fix_str, from_save_folder)
                    return
                else:
                    index += 1
                    continue
            else:
                index += 1
                continue
        print('no available files founded')
        return      
    
    def board_scalars(self, 
                      key,     
                      loss_total, 
                      loss_classifier, 
                      loss_box_reg, 
                      loss_mask, 
                      loss_objectness, 
                      loss_rpn_box_reg, 
                      loss_mimicking_cls,
                      loss_mimicking_cos_sim):
        self.writer.add_scalar('{}_loss_total'.format(key), loss_total, self.step)
        self.writer.add_scalar('{}_loss_classifier'.format(key), loss_classifier, self.step)
        self.writer.add_scalar('{}_loss_box_reg'.format(key), loss_box_reg, self.step)
        self.writer.add_scalar('{}_loss_mask'.format(key), loss_mask, self.step)
        self.writer.add_scalar('{}_loss_objectness'.format(key), loss_objectness, self.step)
        self.writer.add_scalar('{}_loss_rpn_box_reg'.format(key), loss_rpn_box_reg, self.step)
        self.writer.add_scalar('{}_loss_mimicking_cls'.format(key), loss_mimicking_cls, self.step)
        self.writer.add_scalar('{}_loss_mimicking_cos_sim'.format(key), loss_mimicking_cos_sim, self.step)
    
    def train(self, resume = False, from_save_folder = False):
        if resume:
            self.resume_training_load(from_save_folder)
        self.logger.info("Start training")
        meters = MetricLogger(delimiter="  ")
        max_iter = len(self.train_loader)
        
        self.model.train()
        start_training_time = time.time()
        end = time.time()
        
        running_loss = 0.
        running_loss_classifier = 0.
        running_loss_box_reg = 0.
        running_loss_mask = 0.
        running_loss_objectness = 0.
        running_loss_rpn_box_reg = 0.
        running_loss_mimicking_cls = 0.
        running_loss_mimicking_cos_sim = 0.
        
        start_step = self.step
        for _, (images, targets, _) in tqdm(enumerate(self.train_loader, start_step)):
            data_time = time.time() - end
            self.step += 1
            self.schedule_lr()

            images = images.to(self.device)
            targets = [target.to(self.device) for target in targets]

            loss_dict = self.model(images, targets)

            losses = sum(loss for loss in loss_dict.values())

            self.optimizer.zero_grad()
            losses.backward()
            self.optimizer.step()
            
            torch.cuda.empty_cache()
            
            meters.update(loss=losses, **loss_dict)
            running_loss += losses.item()
            running_loss_classifier += loss_dict['loss_classifier']
            running_loss_box_reg += loss_dict['loss_box_reg']
            running_loss_mask += loss_dict['loss_mask']
            running_loss_objectness += loss_dict['loss_objectness']
            running_loss_rpn_box_reg += loss_dict['loss_rpn_box_reg']
            running_loss_mimicking_cls += loss_dict['loss_mimicking_cls']
            running_loss_mimicking_cos_sim += loss_dict['loss_mimicking_cos_sim']
            
            if self.step != 0:
                if self.step % self.board_loss_every == 0:
                    self.board_scalars('train', 
                                        running_loss / self.board_loss_every, 
                                        running_loss_classifier / self.board_loss_every, 
                                        running_loss_box_reg / self.board_loss_every,
                                        running_loss_mask / self.board_loss_every, 
                                        running_loss_objectness / self.board_loss_every,
                                        running_loss_rpn_box_reg / self.board_loss_every, 
                                        running_loss_mimicking_cls / self.board_loss_every,
                                        running_loss_mimicking_cos_sim / self.board_loss_every)
                    running_loss = 0.
                    running_loss_classifier = 0.
                    running_loss_box_reg = 0.
                    running_loss_mask = 0.
                    running_loss_objectness = 0.
                    running_loss_rpn_box_reg = 0.
                    running_loss_mimicking_cls = 0.
                    running_loss_mimicking_cos_sim = 0.
                
                if self.step % self.evaluate_every == 0:
                    val_loss, val_loss_classifier, \
                    val_loss_box_reg, \
                    val_loss_mask, \
                    val_loss_objectness, \
                    val_loss_rpn_box_reg, \
                    val_loss_mimicking_cls, \
                    val_loss_mimicking_cos_sim= self.evaluate(num = self.cfg.SOLVER.EVAL_NUM)
                    self.set_training() 
                    self.board_scalars('val', 
                                        val_loss, 
                                        val_loss_classifier, 
                                        val_loss_box_reg, 
                                        val_loss_mask,
                                        val_loss_objectness,
                                        val_loss_rpn_box_reg,
                                        val_loss_mimicking_cls,
                                        val_loss_mimicking_cos_sim)
                    
                if self.step % self.board_pred_image_every == 0:
                    self.model.eval()
                    for i in range(20):
                        img_path = Path(self.val_loader.dataset.root)/self.val_loader.dataset.get_img_info(1)['file_name']
                        cv_img = cv2.imread(str(img_path))
                        predicted_img = self.predictor.run_on_opencv_image(cv_img)
                        self.writer.add_image('pred_image_{}'.format(i), predicted_img, global_step=self.step)
                    self.model.train()
                
                if self.step % self.inference_every == 0:
                        try:
                            self.model.eval()
                            with torch.no_grad():
                                cocoEval = inference(self.model, self.val_loader, 'coco2014', iou_types=['bbox', 'segm'])[0]
                            self.model.train()
                            bbox_map05 = cocoEval['bbox']['AP50']
                            bbox_mmap = cocoEval['bbox']['AP']
                            segm_map05 = cocoEval['segm']['AP50']
                            segm_mmap = cocoEval['segm']['AP']
                        except:
                            print('eval on coco failed')
                            bbox_map05 = -1
                            bbox_mmap = -1
                            segm_map05 = -1
                            segm_mmap = -1
                        self.writer.add_scalar('bbox_map05', bbox_map05, self.step)
                        self.writer.add_scalar('bbox_mmap', bbox_mmap, self.step)
                        self.writer.add_scalar('segm_map05', segm_map05, self.step)
                        self.writer.add_scalar('segm_mmap', segm_mmap, self.step)
            
            batch_time = time.time() - end
            end = time.time()
            meters.update(time=batch_time, data=data_time)

            eta_seconds = meters.time.global_avg * (max_iter - self.step)
            eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))

            if self.step % 20 == 0 or self.step == max_iter:
                self.logger.info(
                    meters.delimiter.join(
                        [
                            "eta: {eta}",
                            "iter: {iter}",
                            "{meters}",
                            "lr: {lr:.6f}",
                            "max mem: {memory:.0f}",
                        ]
                    ).format(
                        eta=eta_string,
                        iter=self.step,
                        meters=str(meters),
                        lr=self.optimizer.param_groups[0]["lr"],
                        memory=torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                    )
                )
            if self.step >= max_iter:
                return