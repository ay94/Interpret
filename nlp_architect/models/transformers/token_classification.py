# ******************************************************************************
# Copyright 2017-2019 Intel Corporation
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
# ******************************************************************************
import logging
from typing import List, Union

import torch
from torch.nn import CrossEntropyLoss, Dropout, Linear
from torch.nn import functional as F
from torch.utils.data import DataLoader, SequentialSampler, TensorDataset
from transformers import (ROBERTA_PRETRAINED_MODEL_ARCHIVE_MAP,
                          BertForTokenClassification, BertPreTrainedModel,
                          RobertaConfig, RobertaModel, XLNetModel,
                          XLNetPreTrainedModel)

from nlp_architect.data.sequential_tagging import TokenClsInputExample
from nlp_architect.models.transformers.base_model import (InputFeatures,
                                                          TransformerBase)
from nlp_architect.models.transformers.quantized_bert import \
    QuantizedBertForTokenClassification
from nlp_architect.utils.metrics import tagging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _bert_token_tagging_head_fw(bert, input_ids, token_type_ids=None, attention_mask=None,
                                labels=None, position_ids=None, head_mask=None, valid_ids=None):
    outputs = bert.bert(
        input_ids,
        token_type_ids=token_type_ids,
        attention_mask=attention_mask,
        head_mask=head_mask)
    sequence_output = outputs[0]
    sequence_output = bert.dropout(sequence_output)
    logits = bert.classifier(sequence_output)

    if labels is not None:
        loss_fct = CrossEntropyLoss(ignore_index=0)
        active_positions = valid_ids.view(-1) != 0.0
        active_labels = labels.view(-1)[active_positions]
        active_logits = logits.view(-1, bert.num_labels)[active_positions]
        loss = loss_fct(active_logits, active_labels)
        return (loss, logits,)
    return (logits,)


class BertTokenClassificationHead(BertForTokenClassification):
    """BERT token classification head with linear classifier.
       This head's forward ignores word piece tokens in its linear layer.

       The forward requires an additional 'valid_ids' map that maps the tensors
       for valid tokens (e.g., ignores additional word piece tokens generated by
       the tokenizer, as in NER task the 'X' label).
    """

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None,
                position_ids=None, head_mask=None, valid_ids=None):
        return _bert_token_tagging_head_fw(self, input_ids, token_type_ids=token_type_ids,
                                           attention_mask=attention_mask, labels=labels,
                                           position_ids=position_ids, head_mask=head_mask,
                                           valid_ids=valid_ids)


class QuantizedBertForTokenClassificationHead(QuantizedBertForTokenClassification):
    """Quantized BERT token classification head with linear classifier.
       This head's forward ignores word piece tokens in its linear layer.

       The forward requires an additional 'valid_ids' map that maps the tensors
       for valid tokens (e.g., ignores additional word piece tokens generated by
       the tokenizer, as in NER task the 'X' label).
    """

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None,
                position_ids=None, head_mask=None, valid_ids=None):
        return _bert_token_tagging_head_fw(self, input_ids, token_type_ids=token_type_ids,
                                           attention_mask=attention_mask, labels=labels,
                                           position_ids=position_ids, head_mask=head_mask,
                                           valid_ids=valid_ids)


class XLNetTokenClassificationHead(XLNetPreTrainedModel):
    """XLNet token classification head with linear classifier.
       This head's forward ignores word piece tokens in its linear layer.

       The forward requires an additional 'valid_ids' map that maps the tensors
       for valid tokens (e.g., ignores additional word piece tokens generated by
       the tokenizer, as in NER task the 'X' label).
    """

    def __init__(self, config):
        super(XLNetTokenClassificationHead, self).__init__(config)
        self.num_labels = config.num_labels

        self.transformer = XLNetModel(config)
        self.logits_proj = torch.nn.Linear(config.d_model, config.num_labels)
        self.dropout = torch.nn.Dropout(config.dropout)

        self.apply(self.init_weights)

    def forward(self, input_ids, token_type_ids=None, input_mask=None, attention_mask=None,
                mems=None, perm_mask=None, target_mapping=None,
                labels=None, head_mask=None, valid_ids=None):
        transformer_outputs = self.transformer(
            input_ids, token_type_ids=token_type_ids, input_mask=input_mask,
            attention_mask=attention_mask, mems=mems, perm_mask=perm_mask,
            target_mapping=target_mapping, head_mask=head_mask)
        sequence_output = transformer_outputs[0]
        output = self.dropout(sequence_output)
        logits = self.logits_proj(output)

        if labels is not None:
            loss_fct = CrossEntropyLoss(ignore_index=0)
            active_positions = valid_ids.view(-1) != 0.0
            active_labels = labels.view(-1)[active_positions]
            active_logits = logits.view(-1, self.num_labels)[active_positions]
            loss = loss_fct(active_logits, active_labels)
            return (loss, logits,)
        return (logits,)


class RobertaForTokenClassificationHead(BertPreTrainedModel):
    """RoBERTa token classification head with linear classifier.
       This head's forward ignores word piece tokens in its linear layer.

       The forward requires an additional 'valid_ids' map that maps the tensors
       for valid tokens (e.g., ignores additional word piece tokens generated by
       the tokenizer, as in NER task the 'X' label).
    """
    config_class = RobertaConfig
    pretrained_model_archive_map = ROBERTA_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "roberta"

    def __init__(self, config):
        super(RobertaForTokenClassificationHead, self).__init__(config)
        self.num_labels = config.num_labels

        self.roberta = RobertaModel(config)
        self.dropout = Dropout(config.hidden_dropout_prob)
        self.classifier = Linear(config.hidden_size, config.num_labels)

        self.init_weights()

    def forward(self, input_ids, attention_mask=None, token_type_ids=None,
                position_ids=None, head_mask=None, labels=None, valid_ids=None):

        outputs = self.roberta(input_ids,
                               attention_mask=attention_mask,
                               token_type_ids=token_type_ids,
                               position_ids=position_ids,
                               head_mask=head_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if labels is not None:
            loss_fct = CrossEntropyLoss(ignore_index=0)
            active_positions = valid_ids.view(-1) != 0.0
            active_labels = labels.view(-1)[active_positions]
            active_logits = logits.view(-1, self.num_labels)[active_positions]
            loss = loss_fct(active_logits, active_labels)
            return (loss, logits,)
        return (logits,)


class TransformerTokenClassifier(TransformerBase):
    """
    Transformer word tagging classifier

    Args:
        model_type(str): model family (classifier head), choose between bert/quant_bert/xlnet
        labels (List[str], optional): list of tag labels
    """
    MODEL_CLASS = {
        'bert': BertTokenClassificationHead,
        'quant_bert': QuantizedBertForTokenClassificationHead,
        'xlnet': XLNetTokenClassificationHead,
        'roberta': RobertaForTokenClassificationHead,
    }

    def __init__(
            self, model_type: str, labels: List[str] = None, bilou: bool = False,
            *args, load_quantized=False, **kwargs):
        assert model_type in self.MODEL_CLASS.keys(), "unsupported model type"
        self.labels = labels
        self.num_labels = len(labels) + 1  # +1 for padding label
        self.labels_id_map = {k: v for k, v in enumerate(self.labels, 1)}
        self.bilou_format = bilou

        super(TransformerTokenClassifier, self).__init__(model_type,
                                                         labels=self.labels,
                                                         num_labels=self.num_labels, *args,
                                                         **kwargs)

        self.model_class = self.MODEL_CLASS[model_type]
        if model_type == 'quant_bert' and load_quantized:
            self.model = self.model_class.from_pretrained(self.model_name_or_path, from_tf=bool(
                '.ckpt' in self.model_name_or_path), config=self.config, from_8bit=load_quantized)
        else:
            self.model = self.model_class.from_pretrained(self.model_name_or_path, from_tf=bool(
                '.ckpt' in self.model_name_or_path), config=self.config)
        self.to(self.device, self.n_gpus)

    def train(self,
              train_data_set: DataLoader,
              dev_data_set: Union[DataLoader, List[DataLoader]] = None,
              test_data_set: Union[DataLoader, List[DataLoader]] = None,
              gradient_accumulation_steps: int = 1,
              per_gpu_train_batch_size: int = 8,
              max_steps: int = -1,
              num_train_epochs: int = 3,
              max_grad_norm: float = 1.0,
              logging_steps: int = 50,
              save_steps: int = 100,
              training_args: bool = None):
        """
        Run model training

        Args:
            train_data_set (DataLoader): training dataset
            dev_data_set (Union[DataLoader, List[DataLoader]], optional): development data set
            (can be list). Defaults to None.
            test_data_set (Union[DataLoader, List[DataLoader]], optional): test data set
            (can be list). Defaults to None.
            gradient_accumulation_steps (int, optional): gradient accumulation steps.
            Defaults to 1.
            per_gpu_train_batch_size (int, optional): per GPU train batch size (or GPU).
            Defaults to 8.
            max_steps (int, optional): max steps for training. Defaults to -1.
            num_train_epochs (int, optional): number of training epochs. Defaults to 3.
            max_grad_norm (float, optional): max gradient norm. Defaults to 1.0.
            logging_steps (int, optional): number of steps between logging. Defaults to 50.
            save_steps (int, optional): number of steps between model save. Defaults to 100.
        """
        self._train(train_data_set,
                    dev_data_set,
                    test_data_set,
                    gradient_accumulation_steps,
                    per_gpu_train_batch_size,
                    max_steps,
                    num_train_epochs,
                    max_grad_norm,
                    logging_steps=logging_steps,
                    save_steps=save_steps,
                    training_args=training_args)

    def _batch_mapper(self, batch):
        mapping = {'input_ids': batch[0],
                   'attention_mask': batch[1],
                   # XLM don't use segment_ids
                   'token_type_ids': batch[2],
                   'valid_ids': batch[3]}
        if len(batch) > 4:
            mapping.update({'labels': batch[4]})
        return mapping

    def evaluate_predictions(self, logits, label_ids):
        """
        Run evaluation of given logist and truth labels

        Args:
            logits: model logits
            label_ids: truth label ids
        """
        active_positions = label_ids.view(-1) != 0.0
        active_labels = label_ids.view(-1)[active_positions]
        active_logits = logits.view(-1, len(self.labels_id_map) + 1)[active_positions]
        logits = torch.argmax(F.log_softmax(active_logits, dim=1), dim=1)
        logits = logits.detach().cpu().numpy()
        out_label_ids = active_labels.detach().cpu().numpy()
        _, _, f1 = self.extract_labels(out_label_ids, self.labels_id_map, logits, self.bilou_format)
        logger.info("Results on evaluation set: F1 = {}".format(f1))
        return f1

    @staticmethod
    def extract_labels(label_ids, label_map, logits, bilou=False):
        y_true = []
        y_pred = []
        for p, y in zip(logits, label_ids):
            y_pred.append(label_map.get(p, 'O'))
            y_true.append(label_map.get(y, 'O'))
        assert len(y_true) == len(y_pred)
        return tagging(y_pred, y_true, bilou)

    def convert_to_tensors(self,
                           examples: List[TokenClsInputExample],
                           max_seq_length: int = 128,
                           include_labels: bool = True) -> TensorDataset:
        """
        Convert examples to tensor dataset

        Args:
            examples (List[SequenceClsInputExample]): examples
            max_seq_length (int, optional): max sequence length. Defaults to 128.
            include_labels (bool, optional): include labels. Defaults to True.

        Returns:
            TensorDataset:
        """
        features = self._convert_examples_to_features(
            examples, max_seq_length, self.tokenizer, include_labels,
            # xlnet has a cls token at the end
            cls_token_at_end=bool(
                self.model_type in [
                    'xlnet']), cls_token=self.tokenizer.cls_token,
            cls_token_segment_id=2 if self.model_type in[
                'xlnet'] else 0, sep_token=self.tokenizer.sep_token,
            sep_token_extra=bool(self.model_type in ['roberta']),
            # pad on the left for xlnet
            pad_on_left=bool(
                self.model_type in ['xlnet']), pad_token=self.tokenizer.convert_tokens_to_ids(
                    [
                        self.tokenizer.pad_token
                    ])[0],
            pad_token_segment_id=4 if self.model_type in ['xlnet'] else 0)
        # Convert to Tensors and build dataset
        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
        all_valid_ids = torch.tensor([f.valid_ids for f in features], dtype=torch.long)

        if include_labels:
            all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.long)
            dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids,
                                    all_valid_ids, all_label_ids)
        else:
            dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids,
                                    all_valid_ids)
        return dataset

    def _convert_examples_to_features(self,
                                      examples: List[TokenClsInputExample],
                                      max_seq_length,
                                      tokenizer,
                                      include_labels=True,
                                      cls_token_at_end=False, pad_on_left=False,
                                      cls_token='[CLS]', sep_token='[SEP]', pad_token=0,
                                      sequence_segment_id=0,
                                      sep_token_extra=0,
                                      cls_token_segment_id=1, pad_token_segment_id=0,
                                      mask_padding_with_zero=True):
        """ Loads a data file into a list of `InputBatch`s
            `cls_token_at_end` define the location of the CLS token:
                - False (Default, BERT/XLM pattern): [CLS] + A + [SEP] + B + [SEP]
                - True (XLNet/GPT pattern): A + [SEP] + B + [SEP] + [CLS]
            `cls_token_segment_id` define the segment id associated to the CLS token
            (0 for BERT, 2 for XLNet)
        """

        if include_labels:
            label_map = {v: k for k, v in self.labels_id_map.items()}
            label_pad = 0

        features = []
        for (ex_index, example) in enumerate(examples):
            if ex_index % 10000 == 0:
                logger.info("Processing example %d of %d", ex_index, len(examples))

            tokens = []
            labels = []
            valid_tokens = []
            for i, token in enumerate(example.tokens):
                new_tokens = tokenizer.tokenize(token)
                tokens.extend(new_tokens)
                v_tok = [0] * (len(new_tokens))
                v_tok[0] = 1
                valid_tokens.extend(v_tok)
                if include_labels:
                    v_lbl = [label_pad] * (len(new_tokens))
                    v_lbl[0] = label_map.get(example.label[i])
                    labels.extend(v_lbl)

            # truncate by max_seq_length
            special_tokens_count = 3 if sep_token_extra else 2
            tokens = tokens[:(max_seq_length - special_tokens_count)]
            valid_tokens = valid_tokens[:(max_seq_length - special_tokens_count)]
            if include_labels:
                labels = labels[:(max_seq_length - special_tokens_count)]

            tokens += [sep_token]
            if include_labels:
                labels += [label_pad]
            valid_tokens += [0]
            if sep_token_extra:  # roberta special case
                tokens += [sep_token]
                valid_tokens += [0]
                if include_labels:
                    labels += [label_pad]
            segment_ids = [sequence_segment_id] * len(tokens)

            if cls_token_at_end:
                tokens = tokens + [cls_token]
                segment_ids = segment_ids + [cls_token_segment_id]
                if include_labels:
                    labels = labels + [label_pad]
                valid_tokens = valid_tokens + [0]
            else:
                tokens = [cls_token] + tokens
                segment_ids = [cls_token_segment_id] + segment_ids
                if include_labels:
                    labels = [label_pad] + labels
                valid_tokens = [0] + valid_tokens

            input_ids = tokenizer.convert_tokens_to_ids(tokens)

            input_mask = [1 if mask_padding_with_zero else 0] * len(input_ids)

            # Zero-pad up to the sequence length.
            padding_length = max_seq_length - len(input_ids)
            if pad_on_left:
                input_ids = ([pad_token] * padding_length) + input_ids
                input_mask = ([0 if mask_padding_with_zero else 1] * padding_length) + input_mask
                segment_ids = ([pad_token_segment_id] * padding_length) + segment_ids
                if include_labels:
                    labels = ([label_pad] * padding_length) + labels
                valid_tokens = ([0] * padding_length) + valid_tokens
            else:
                input_ids = input_ids + ([pad_token] * padding_length)
                input_mask = input_mask + ([0 if mask_padding_with_zero else 1] * padding_length)
                segment_ids = segment_ids + ([pad_token_segment_id] * padding_length)
                if include_labels:
                    labels = labels + ([label_pad] * padding_length)
                valid_tokens = valid_tokens + ([0] * padding_length)

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length
            assert len(valid_tokens) == max_seq_length
            if include_labels:
                assert len(labels) == max_seq_length

            features.append(InputFeatures(input_ids=input_ids,
                                          input_mask=input_mask,
                                          segment_ids=segment_ids,
                                          label_id=labels,
                                          valid_ids=valid_tokens))
        return features

    def inference(self, examples: List[TokenClsInputExample], max_seq_length: int, batch_size: int = 64):
        """
        Run inference on given examples

        Args:
            examples (List[SequenceClsInputExample]): examples
            batch_size (int, optional): batch size. Defaults to 64.

        Returns:
            logits
        """
        data_set = self.convert_to_tensors(
            examples, max_seq_length=max_seq_length, include_labels=False)
        inf_sampler = SequentialSampler(data_set)
        inf_dataloader = DataLoader(data_set, sampler=inf_sampler, batch_size=batch_size)
        logits = self._evaluate(inf_dataloader)
        active_positions = data_set.tensors[-1].view(len(data_set), -1) != 0.0
        logits = torch.argmax(F.log_softmax(logits, dim=2), dim=2)
        res_ids = []
        for i in range(logits.size()[0]):
            res_ids.append(logits[i][active_positions[i]].detach().cpu().numpy())
        output = []
        for tag_ids, ex in zip(res_ids, examples):
            tokens = ex.tokens
            tags = [self.labels_id_map.get(t, 'O') for t in tag_ids]
            output.append((tokens, tags))
        return output
