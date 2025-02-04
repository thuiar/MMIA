import torch
import torch.nn.functional as F
import logging
from torch import nn
from utils.functions import restore_model, save_model, EarlyStopping
from tqdm import trange, tqdm
from utils.metrics import AverageMeter, Metrics, OOD_Metrics, OID_Metrics
from data.utils import get_dataloader
import numpy as np
from torch import optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
from sklearn.neighbors import LocalOutlierFactor
import pandas as pd
from scipy.stats import norm as dist_model
from evaluation.oos_cls import doc_classification
from evaluation.ood_det import ood_detection


__all__ = ['MULT']

class MULT:

    def __init__(self, args, data, model):

        self.logger = logging.getLogger(args.logger_name)
        
        # self.device, self.model = model.device, model.model
        self.device, self.model = model.device, model._set_model(args)
        self.optimizer = optim.Adam(self.model.parameters(), lr = args.lr)
        self.scheduler = ReduceLROnPlateau(self.optimizer, mode='max', factor=0.1, verbose=True, patience=args.wait_patience)

        mm_data = data.data
        mm_dataloader = get_dataloader(args, mm_data)
        self.train_dataloader, self.eval_dataloader, self.test_dataloader = \
            mm_dataloader['train'], mm_dataloader['dev'], mm_dataloader['test']
        
        # self.device, self.model = model.device, model._set_model(args)

        # self.optimizer = optim.Adam(self.model.parameters(), lr = args.lr)
        # self.scheduler = ReduceLROnPlateau(self.optimizer, mode='max', factor=0.1, verbose=True, patience=args.wait_patience)
        
        self.args = args
        self.criterion = nn.CrossEntropyLoss()
        self.metrics = Metrics(args)
        self.oid_metrics = OID_Metrics(args)
        self.ood_metrics = OOD_Metrics(args)
        
        if args.train:
            self.best_eval_score = 0
        else:
            self.model = restore_model(self.model, args.model_output_path, self.device)

    def _train(self, args): 

        early_stopping = EarlyStopping(args)

        for epoch in trange(int(args.num_train_epochs), desc="Epoch"):
            self.model.train()
            loss_record = AverageMeter()
            
            for step, batch in enumerate(tqdm(self.train_dataloader, desc="Iteration")):

                text_feats = batch['text_feats'].to(self.device)
                video_feats = batch['video_feats'].to(self.device)
                audio_feats = batch['audio_feats'].to(self.device)
                label_ids = batch['label_ids'].to(self.device)

                with torch.set_grad_enabled(True):

                    preds, last_hiddens = self.model(text_feats, video_feats, audio_feats)
             
                    loss = self.criterion(preds, label_ids)

                    self.optimizer.zero_grad()
                    
                    loss.backward()
                    loss_record.update(loss.item(), label_ids.size(0))

                    if args.grad_clip != -1.0:
                        nn.utils.clip_grad_value_([param for param in self.model.parameters() if param.requires_grad], args.grad_clip)

                    self.optimizer.step()
            
            outputs = self._get_outputs(args, mode = 'eval')
            eval_score = outputs[args.eval_monitor]
            self.scheduler.step(eval_score)


            eval_results = {
                'train_loss': round(loss_record.avg, 4),
                'best_eval_score': round(early_stopping.best_score, 4),
                'eval_score': round(eval_score, 4),
            }

            self.logger.info("***** Epoch: %s: Eval results *****", str(epoch + 1))
            for key in sorted(eval_results.keys()):
                self.logger.info("  %s = %s", key, str(eval_results[key]))
            
            early_stopping(eval_score, self.model)

            if early_stopping.early_stop:
                self.logger.info(f'EarlyStopping at epoch {epoch + 1}')
                break

        self.best_eval_score = early_stopping.best_score
        self.model = early_stopping.best_model   
        
        if args.save_model:
            self.logger.info('Trained models are saved in %s', args.model_output_path)
            save_model(self.model, args.model_output_path)   

    def _get_outputs(self, args, mode = 'eval', return_sample_results = False, show_results = False,test_ind = False):
        
        if mode == 'eval':
            dataloader = self.eval_dataloader
        elif mode == 'test':
            dataloader = self.test_dataloader
        elif mode == 'train':
            dataloader = self.train_dataloader

        self.model.eval()

        total_labels = torch.empty(0,dtype=torch.long).to(self.device)
        total_preds = torch.empty(0,dtype=torch.long).to(self.device)
        total_features = torch.empty((0, self.model.model.combined_dim)).to(self.device)
        total_logits = torch.empty((0, args.num_labels)).to(self.device)
        
        loss_record = AverageMeter()

        for batch in tqdm(dataloader, desc="Iteration"):

            text_feats = batch['text_feats'].to(self.device)
            video_feats = batch['video_feats'].to(self.device)
            audio_feats = batch['audio_feats'].to(self.device)
            label_ids = batch['label_ids'].to(self.device)
            
            with torch.set_grad_enabled(False):
                
                logits, last_hiddens = self.model(text_feats, video_feats, audio_feats)

                total_logits = torch.cat((total_logits, logits))
                total_features = torch.cat((total_features, last_hiddens))
                total_labels = torch.cat((total_labels, label_ids))

                if mode == 'eval':
                    loss = self.criterion(logits, label_ids)
                    loss_record.update(loss.item(), label_ids.size(0))
                print(f"logits shape: {logits.shape}")
                print(f"label_ids shape: {label_ids.shape}")
                print(f"label_ids max value: {label_ids.max().item()}")
                print(f"last_hiddens shape: {last_hiddens.shape}")

        total_probs = F.softmax(total_logits.detach(), dim=1)
        total_maxprobs, total_preds = total_probs.max(dim = 1)

        y_logit = torch.sigmoid(total_logits.detach()).cpu().numpy()
        y_pred = total_preds.cpu().numpy()
        y_true = total_labels.cpu().numpy()
        y_feat = total_features.cpu().numpy()
        y_prob = total_maxprobs.cpu().numpy()

        #outputs = self.metrics(y_true, y_pred, show_results=show_results)

        if test_ind:
            outputs = self.metrics(y_true[y_true != args.ood_label_id], y_pred[y_true != args.ood_label_id])
        else:
            outputs = self.metrics(y_true, y_pred, show_results = show_results)

        if mode == 'eval':
            outputs.update({'loss': loss_record.avg})

        outputs.update(
            {
                'y_prob': y_prob,
                'y_logit': y_logit,
                'y_feat': y_feat,
                'y_true': y_true,
                'y_pred': y_pred
            }
        )

        return outputs

    def _test(self, args):

        test_results = {}
        
        ind_test_results = self._get_outputs(args, mode = 'test', return_sample_results = False, show_results = False, test_ind = True)
        if args.train:
            test_results['best_eval_score'] = round(self.best_eval_score, 4)
        test_results.update(ind_test_results)
  
        if args.test_ood:
            
            if args.test_mode == 'ood_cls':
                tmp_outputs = self._get_outputs(args, mode = 'test')
                ind_train_outputs = self._get_outputs(args, mode = 'train')

                inputs = {
                    'y_logit_train': ind_train_outputs['y_logit'],
                    'y_true_train': ind_train_outputs['y_true'],
                    'y_true_test': tmp_outputs['y_true'],
                    'y_logit_test': tmp_outputs['y_logit']
                }
                
                oid_test_results = doc_classification(args, inputs)
                test_results.update(oid_test_results)

            if args.test_mode == 'ood_det':
                tmp_outputs = self._get_outputs(args, mode = 'test')
                if args.ood_detection_method in ['residual', 'ma', 'vim']:
                    ind_train_outputs = self._get_outputs(args, mode = 'train')
                    
                    tmp_outputs['train_feats'] = ind_train_outputs['y_feat']
                    tmp_outputs['train_labels'] = ind_train_outputs['y_true']
                    
                    w, b = self.model.vim()
                    tmp_outputs['w'] = w
                    tmp_outputs['b'] = b
                
                ood_test_scores = ood_detection(args , tmp_outputs)  
                test_results.update(ood_test_scores)
    
        return test_results