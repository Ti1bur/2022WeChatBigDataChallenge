# %%writefile qqmodel/qq_uni_model.py
import math
import random
import torch
import torch.nn as nn
import torch.nn.functional as F

import sys
from swin import swin_base
sys.path.append("..")
from masklm import MaskLM, MaskVideo, ShuffleVideo
from transformers.models.bert.modeling_bert import BertConfig, BertOnlyMLMHead
from transformers.models.bert.modeling_bert import BertPreTrainedModel, BertEmbeddings, BertEncoder
import pdb

class MultiModalForPretrain(nn.Module):
    def __init__(self, model_path, task=['mlm', 'mvm', 'itm'], init_from_pretrain=True):
        super().__init__()
        uni_bert_cfg = BertConfig.from_pretrained(model_path)
        # uni_bert_cfg.num_hidden_layers = 1
        #self.visiual_backbone = swin_base()
        self.video_fc = nn.Linear(512, 768)
        self.newfc_hidden = torch.nn.Linear(uni_bert_cfg.hidden_size, 768)
        
        self.task = set(task)

        if 'mlm' in task:
            self.lm = MaskLM(tokenizer_path=model_path)
            self.vocab_size = uni_bert_cfg.vocab_size

        if 'mvm' in task:
            self.vm = MaskVideo()
            self.roberta_mvm_lm_header = VisualOnlyMLMHead(uni_bert_cfg)

        if 'itm' in task:
            self.sv = ShuffleVideo()
            self.newfc_itm = torch.nn.Linear(uni_bert_cfg.hidden_size, 1)

        if init_from_pretrain:
            self.roberta = UniBertForMaskedLM.from_pretrained(model_path, config=uni_bert_cfg)
        else:
            self.roberta = UniBertForMaskedLM(uni_bert_cfg)

    def forward(self, video_feature, video_mask, text_input_ids, text_mask, target=None, task=None):
        loss, pred = 0, None
        video_feature = self.video_fc(video_feature)
        if task is None:
            sample_task = self.task
        elif type(task) == str:
            sample_task = [task]
        elif type(task) == list:
            sample_task = task

        # perprocess
        return_mlm = False
        if 'mlm' in sample_task:
            input_ids, lm_label = self.lm.torch_mask_tokens(text_input_ids.cpu())
            text_input_ids = input_ids.to(text_input_ids.device)
            lm_label = lm_label.to(text_input_ids.device)
            return_mlm = True

        if 'mvm' in sample_task:
            vm_input = video_feature
            input_feature, video_label = self.vm.torch_mask_frames(video_feature.cpu(), video_mask.cpu())
            video_feature = input_feature.to(video_feature.device)
            video_label = video_label.to(video_feature.device)

        if 'itm' in sample_task:
            input_feature, video_text_match_label = self.sv.torch_shuf_video(video_feature.cpu())
            video_feature = input_feature.to(video_feature.device)
            video_text_match_label = video_text_match_label.to(video_feature.device)

        # concat features
        features, lm_prediction_scores = self.roberta(video_feature, video_mask, text_input_ids, text_mask,
                                                      return_mlm=return_mlm)
        features_mean = torch.mean(features, 1)
        embedding = self.newfc_hidden(features_mean)

        normed_embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)

        # compute loss
        if 'mlm' in sample_task:
            pred = lm_prediction_scores.contiguous().view(-1, self.vocab_size)
            masked_lm_loss = nn.CrossEntropyLoss()(pred, lm_label.view(-1))
            loss += masked_lm_loss / 1.25

        if 'mvm' in sample_task:
            vm_output = self.roberta_mvm_lm_header(features[:, :video_feature.size()[1], :])
            masked_vm_loss = self.calculate_mfm_loss(vm_output, vm_input,
                                                     video_mask, video_label, normalize=False)
            loss += masked_vm_loss / 3

        if 'itm' in sample_task:
            text_feature = features[:, video_feature.size()[1]:, :]
            text_feature = text_feature[:, 0, :]
            pred = self.newfc_itm(text_feature)
            itm_loss = nn.BCEWithLogitsLoss()(pred.view(-1), video_text_match_label.view(-1))
            loss += itm_loss
        return loss, masked_lm_loss, masked_vm_loss, itm_loss

    def calculate_mfm_loss(self, video_feature_output, video_feature_input,
                           video_mask, video_labels_index, normalize=False, temp=0.1):
        if normalize:
            video_feature_output = torch.nn.functional.normalize(video_feature_output, p=2, dim=2)
            video_feature_input = torch.nn.functional.normalize(video_feature_input, p=2, dim=2)

        afm_scores_tr = video_feature_output.view(-1, video_feature_output.shape[-1])

        video_tr = video_feature_input.permute(2, 0, 1)
        video_tr = video_tr.view(video_tr.shape[0], -1)

        logits_matrix = torch.mm(afm_scores_tr, video_tr)
        if normalize:
            logits_matrix = logits_matrix / temp

        video_mask_float = video_mask.to(dtype=torch.float)
        mask_matrix = torch.mm(video_mask_float.view(-1, 1), video_mask_float.view(1, -1))
        masked_logits = logits_matrix + (1. - mask_matrix) * -1e2

        logpt = F.log_softmax(masked_logits, dim=-1)
        logpt = torch.diag(logpt)
        nce_loss = -logpt

        video_labels_index_mask = (video_labels_index != -100)
        nce_loss = nce_loss.masked_select(video_labels_index_mask.view(-1))
        nce_loss = nce_loss.mean()
        return nce_loss


def gelu(x):
    """Implementation of the gelu activation function.
    For information: OpenAI GPT's gelu is slightly different (and gives slightly different results):
    0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * torch.pow(x, 3))))
    """
    return x * 0.5 * (1.0 + torch.erf(x / math.sqrt(2.0)))


def swish(x):
    return x * torch.sigmoid(x)


ACT2FN = {"gelu": gelu, "relu": torch.nn.functional.relu, "swish": swish}


class VisualPredictionHeadTransform(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        if isinstance(config.hidden_act, str):
            self.transform_act_fn = ACT2FN[config.hidden_act]
        else:
            self.transform_act_fn = config.hidden_act
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.transform_act_fn(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        return hidden_states


class VisualLMPredictionHead(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.transform = VisualPredictionHeadTransform(config)

        # The output weights are the same as the input embeddings, but there is
        # an output-only bias for each token.
        self.decoder = nn.Linear(config.hidden_size, 768, bias=False)
        self.bias = nn.Parameter(torch.zeros(768))

        # Need a link between the two variables so that the bias is correctly resized with `resize_token_embeddings`
        self.decoder.bias = self.bias

    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        hidden_states = self.decoder(hidden_states)
        return hidden_states


class VisualOnlyMLMHead(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.predictions = VisualLMPredictionHead(config)

    def forward(self, sequence_output):
        prediction_scores = self.predictions(sequence_output)
        return prediction_scores


class UniBertForMaskedLM(BertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.bert = UniBert(config)
        self.cls = BertOnlyMLMHead(config)

    # Copied from transformers.models.bert.modeling_bert.BertModel.forward
    def forward(self, video_feature, video_mask, text_input_ids, text_mask, gather_index=None, return_mlm=False):
        encoder_outputs = self.bert(video_feature, video_mask, text_input_ids, text_mask)
        if return_mlm:
            return encoder_outputs, self.cls(encoder_outputs)[:, video_feature.size()[1]:, :]
        else:
            return encoder_outputs, None


class UniBert(BertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.config = config

        self.embeddings = BertEmbeddings(config)
        self.encoder = BertEncoder(config)

        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def _prune_heads(self, heads_to_prune):
        for layer, heads in heads_to_prune.items():
            self.encoder.layer[layer].attention.prune_heads(heads)

    # Copied from transformers.models.bert.modeling_bert.BertModel.forward
    def forward(self, video_feature, video_mask, text_input_ids, text_mask, gather_index=None):
        text_emb = self.embeddings(input_ids=text_input_ids)

        video_emb = self.embeddings(inputs_embeds=video_feature)

        embedding_output = torch.cat([video_emb, text_emb], 1)

        mask = torch.cat([video_mask, text_mask], 1)
        mask = mask[:, None, None, :]
        mask = (1.0 - mask) * -10000.0

        encoder_outputs = self.encoder(embedding_output, attention_mask=mask)['last_hidden_state']
        return encoder_outputs
    
