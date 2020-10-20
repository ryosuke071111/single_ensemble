#export CUDA_VISIBLE_DEVICES=1
import tempfile
from typing import Dict, Iterable, List, Tuple
import os

import allennlp
import torch
from allennlp.data import PyTorchDataLoader, DatasetReader, Instance, Vocabulary
from allennlp.data.dataset_readers import SequenceTaggingDatasetReader, Conll2003DatasetReader
from allennlp.data.fields import LabelField, TextField
from allennlp.data.token_indexers import TokenIndexer, SingleIdTokenIndexer, PretrainedTransformerIndexer, ELMoTokenCharactersIndexer, TokenCharactersIndexer, PretrainedTransformerMismatchedIndexer
from allennlp.data.tokenizers import Token, Tokenizer, WhitespaceTokenizer, PretrainedTransformerTokenizer
from allennlp.models import Model
from allennlp.modules import TextFieldEmbedder, Seq2VecEncoder
from allennlp.modules.seq2vec_encoders import BagOfEmbeddingsEncoder, CnnEncoder
from allennlp.modules.seq2seq_encoders import PytorchTransformer
from allennlp.modules.token_embedders import Embedding, TokenEmbedder, PretrainedTransformerEmbedder, ElmoTokenEmbedder, TokenCharactersEncoder, PretrainedTransformerMismatchedEmbedder
from allennlp.modules.text_field_embedders import BasicTextFieldEmbedder
from allennlp.nn import util
from allennlp.training.trainer import GradientDescentTrainer, Trainer
from allennlp.training.optimizers import AdamOptimizer
from allennlp.training.metrics import CategoricalAccuracy
from allennlp.training.util import evaluate
from allennlp.predictors import Predictor
from allennlp.common import JsonDict
from allennlp.modules.seq2vec_encoders.cnn_encoder import CnnEncoder
from allennlp.modules.seq2vec_encoders.bert_pooler import BertPooler
from allennlp.modules.seq2seq_encoders import LstmSeq2SeqEncoder

from transformers import BertTokenizer

from allennlp_models.tagging.models import CrfTagger
from allennlp_models.generation.dataset_readers import Seq2SeqDatasetReader

from allennlp_models.generation.models import ComposedSeq2Seq
from allennlp_models.generation.modules.decoder_nets import StackedSelfAttentionDecoderNet
from allennlp_models.generation.modules.seq_decoders import AutoRegressiveSeqDecoder
from allennlp.training.metrics import BLEU, Entropy
from allennlp.training.learning_rate_schedulers import LinearWithWarmup


def build_dataset_reader()  -> DatasetReader:
    source_tokenizer = WhitespaceTokenizer()
    target_tokenizer = WhitespaceTokenizer()

    # indexers = {"source_tokens":SingleIdTokenIndexer(), "target_tokens":SingleIdTokenIndexer()}
    source_token_indexers = {"source_tokens":SingleIdTokenIndexer(namespace="source_tokens", lowercase_tokens=True)}
    target_token_indexers = {"target_tokens":SingleIdTokenIndexer(namespace="target_tokens", lowercase_tokens=True)}

    return Seq2SeqDatasetReader(source_tokenizer=source_tokenizer, target_tokenizer=target_tokenizer, \
            source_token_indexers=source_token_indexers, target_token_indexers=target_token_indexers, target_max_tokens=max_len)

def read_data(reader: DatasetReader) -> Tuple[Iterable[Instance], Iterable[Instance]]:
    print("Reading data")
    training_data = reader.read(TRAIN_PATH)
    validation_data = reader.read(DEV_PATH)
    return training_data, validation_data

def build_vocab(instances: Iterable[Instance]) -> Vocabulary:
    print("Building the vocabulary")
    # ret = Vocabulary.from_instances(instances)
    ret = Vocabulary(padding_token = "<pad>", oov_token = "<unk>")
    # ret = ret.from_instances(instances)
    ret.set_from_file(filename="/home/ryosuke/desktop/allen_practice/iwslt15/vocab.en",  namespace="source_tokens", oov_token="<unk>")
    ret.set_from_file(filename="/home/ryosuke/desktop/allen_practice/iwslt15/vocab.vi",  namespace="target_tokens", oov_token="<unk>")
    return ret

def build_model(vocab: Vocabulary) -> Model:
    print("Building the model")
    vocab_size_s = vocab.get_vocab_size("source_tokens")
    vocab_size_t = vocab.get_vocab_size("target_tokens")

    bleu = BLEU(exclude_indices = {0,2,3})

    source_text_embedder = BasicTextFieldEmbedder({"source_tokens": Embedding(embedding_dim=embedding_dim, num_embeddings=vocab_size_s)})
    encoder = PytorchTransformer(input_dim=embedding_dim, num_layers=6, positional_encoding="sinusoidal")

    

    target_text_embedder = Embedding(embedding_dim=embedding_dim, num_embeddings=vocab_size_t)
    decoder_net = StackedSelfAttentionDecoderNet(decoding_dim=embedding_dim, target_embedding_dim=embedding_dim, feedforward_hidden_dim=1024, num_layers=6, num_attention_heads=8)
    decoder_net.decodes_parallel=True
    decoder = AutoRegressiveSeqDecoder(vocab, decoder_net, max_len, target_text_embedder, target_namespace="target_tokens", tensor_based_metric=bleu, scheduled_sampling_ratio=0.0)

    return ComposedSeq2Seq(vocab, source_text_embedder, encoder, decoder)

def build_data_loaders(train_data: torch.utils.data.Dataset, dev_data: torch.utils.data.Dataset) -> Tuple[allennlp.data.PyTorchDataLoader, allennlp.data.PyTorchDataLoader]:
    train_loader = PyTorchDataLoader(train_data, batch_size=batch_size, shuffle=True)
    dev_loader = PyTorchDataLoader(dev_data, batch_size=batch_size, shuffle=True)
    return train_loader, dev_loader

def build_trainer(model: Model, serialization_dir:str, train_loader: PyTorchDataLoader, dev_loader: PyTorchDataLoader) -> Trainer:
    parameters = [[n,p] for n, p in model.named_parameters() if p.requires_grad]
    optimizer = AdamOptimizer(parameters, lr=lr, weight_decay=weight_decay)
    lr_scheduler = LinearWithWarmup(optimizer, num_epoch, warmup_steps=warmup, num_steps_per_epoch=30)
    trainer = GradientDescentTrainer(model=model, serialization_dir=serialization_dir, data_loader=train_loader, \
                                validation_data_loader=dev_loader, num_epochs=num_epoch, optimizer=optimizer, \
                                num_gradient_accumulation_steps=grad_accum,
                                grad_norm=grad_norm, patience=patience)
    return trainer




def run_training_loop():
    dataset_reader = build_dataset_reader()
    train_data, dev_data = read_data(dataset_reader)

    vocab = build_vocab(train_data + dev_data)

    # print('vocab', vocab.get_index_to_token_vocabulary(namespace="source_tokens"))
    print('vocab', vocab)

    model = build_model(vocab)
    model.cuda() if torch.cuda.is_available() else model

    train_data.index_with(vocab)
    dev_data.index_with(vocab)

    train_loader, dev_loader = build_data_loaders(train_data, dev_data)

    # with tempfile.TemporaryDirectory() as serialization_dir:
    trainer = build_trainer(model, serialization_dir, train_loader, dev_loader)
    print("Starting training")
    trainer.train()
    print("Finished training")
    
    return model, dataset_reader

cur_dir = os.getcwd()
TRAIN_PATH = cur_dir + "/wmt/train"
DEV_PATH = cur_dir + "/wmt/test"
TEST_PATH = cur_dir + "/wmt/test"

TRAIN_PATH = "/home/ryosuke/desktop/allen_practice/iwslt15/train"
DEV_PATH = "/home/ryosuke/desktop/allen_practice/iwslt15/valid"
TEST_PATH = "/home/ryosuke/desktop/allen_practice/iwslt15/test"

# TRAIN_PATH = "./data_small/small_japanese"
# DEV_PATH = "./data_small/small_japanese"
# TEST_PATH = "./data_small/small_japanese"


batch_size = 16
embedding_dim = 256
num_epoch = 200
lr = 0.001
# num_labels = 2
grad_accum = 256
weight_decay = 0.00001
num_serialized_models_to_keep = 3
grad_norm = 5.0
patience = None
max_len = 50
warmup = 100

import datetime
now = "{0:%Y%m%d_%H%M%S}".format(datetime.datetime.now())

serialization_dir = "/home/ryosuke/desktop/allen_practice/checkpoints/nmt_lr_" + str(lr) + "_" + now


model, dataset_reader = run_training_loop()
test_data = dataset_reader.read(TEST_PATH)
test_data.index_with(model.vocab)
data_loader = PyTorchDataLoader(test_data, batch_size=32)


results = evaluate(model, data_loader, cuda_device=0)
print(results)
print("batch_size:{}, num_epoch:{}, lr:{}, grad_accum:{}".format(batch_size, num_epoch, lr, grad_accum))