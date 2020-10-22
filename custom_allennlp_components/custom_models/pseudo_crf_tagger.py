from typing import Dict, Optional, List, Any

from overrides import overrides
import torch
from torch.nn.modules.linear import Linear

from allennlp.common.checks import check_dimensions_match, ConfigurationError
# from allennlp.data import Vocabulary
from allennlp.modules import Seq2SeqEncoder, TimeDistributed, TextFieldEmbedder
from allennlp.modules import ConditionalRandomField, FeedForward
from allennlp.modules.conditional_random_field import allowed_transitions
from allennlp.models.model import Model
from allennlp.nn import InitializerApplicator, RegularizerApplicator
import allennlp.nn.util as util
from allennlp.training.metrics import CategoricalAccuracy, SpanBasedF1Measure
from allennlp.data import Vocabulary
# from myallennlp.mymodels.vocabulary import VocabularyOrthogonal
import random
from collections import Counter

import torch.nn.functional as F

# from allennlp.commands.evaluate import make_pseudo_tags

def voting(matrix):
    print('mattix', matrix)
    matrix = torch.tensor(matrix).transpose(0,1).tolist()
    matrix = list(map(lambda x:Counter(x).most_common()[0][0],matrix))
    return matrix

@Model.register("crf_tagger_orthogonal_embedding")
class PseudoCrfTagger(Model):
    """
    The ``PseudoCrfTagger`` encodes a sequence of text with a ``Seq2SeqEncoder``,
    then uses a Conditional Random Field model to predict a tag for each token in the sequence.

    Parameters
    ----------
    vocab : ``Vocabulary``, required
        A Vocabulary, required in order to compute sizes for input/output projections.
    text_field_embedder : ``TextFieldEmbedder``, required
        Used to embed the tokens ``TextField`` we get as input to the model.
    encoder : ``Seq2SeqEncoder``
        The encoder that we will use in between embedding tokens and predicting output tags.
    label_namespace : ``str``, optional (default=``labels``)
        This is needed to compute the SpanBasedF1Measure metric.
        Unless you did something unusual, the default value should be what you want.
    feedforward : ``FeedForward``, optional, (default = None).
        An optional feedforward layer to apply after the encoder.
    label_encoding : ``str``, optional (default=``None``)
        Label encoding to use when calculating span f1 and constraining
        the CRF at decoding time . Valid options are "BIO", "BIOUL", "IOB1", "BMES".
        Required if ``calculate_span_f1`` or ``constrain_crf_decoding`` is true.
    include_start_end_transitions : ``bool``, optional (default=``True``)
        Whether to include start and end transition parameters in the CRF.
    constrain_crf_decoding : ``bool``, optional (default=``None``)
        If ``True``, the CRF is constrained at decoding time to
        produce valid sequences of tags. If this is ``True``, then
        ``label_encoding`` is required. If ``None`` and
        label_encoding is specified, this is set to ``True``.
        If ``None`` and label_encoding is not specified, it defaults
        to ``False``.
    calculate_span_f1 : ``bool``, optional (default=``None``)
        Calculate span-level F1 metrics during training. If this is ``True``, then
        ``label_encoding`` is required. If ``None`` and
        label_encoding is specified, this is set to ``True``.
        If ``None`` and label_encoding is not specified, it defaults
        to ``False``.
    dropout:  ``float``, optional (default=``None``)
    verbose_metrics : ``bool``, optional (default = False)
        If true, metrics will be returned per label class in addition
        to the overall statistics.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.
    """

    def __init__(
        self,
        vocab: Vocabulary,
        text_field_embedder: TextFieldEmbedder,
        encoder: Seq2SeqEncoder,
        label_namespace: str = "labels",
        feedforward: Optional[FeedForward] = None,
        label_encoding: Optional[str] = None,
        include_start_end_transitions: bool = True,
        constrain_crf_decoding: bool = None,
        calculate_span_f1: bool = None,
        dropout: Optional[float] = None,
        verbose_metrics: bool = False,
        initializer: InitializerApplicator = InitializerApplicator(),
        regularizer: Optional[RegularizerApplicator] = None,
        num_virtual_models: int = 0
    ) -> None:
        super().__init__(vocab, regularizer)


        self.num_virtual_models = num_virtual_models

        self.label_namespace = label_namespace
        self.text_field_embedder = text_field_embedder
        self.num_tags = self.vocab.get_vocab_size(label_namespace)
        self.encoder = encoder
        self._verbose_metrics = verbose_metrics
        if dropout:
            self.dropout = torch.nn.Dropout(dropout)
        else:
            self.dropout = None
        self._feedforward = feedforward

        if feedforward is not None:
            output_dim = feedforward.get_output_dim()
        else:
            output_dim = self.encoder.get_output_dim()
        self.tag_projection_layer = TimeDistributed(Linear(output_dim, self.num_tags))

        # if  constrain_crf_decoding and calculate_span_f1 are not
        # provided, (i.e., they're None), set them to True
        # if label_encoding is provided and False if it isn't.
        if constrain_crf_decoding is None:
            constrain_crf_decoding = label_encoding is not None
        if calculate_span_f1 is None:
            calculate_span_f1 = label_encoding is not None

        self.label_encoding = label_encoding
        if constrain_crf_decoding:
            if not label_encoding:
                raise ConfigurationError(
                    "constrain_crf_decoding is True, but " "no label_encoding was specified."
                )
            labels = self.vocab.get_index_to_token_vocabulary(label_namespace)
            constraints = allowed_transitions(label_encoding, labels)
        else:
            constraints = None

        self.include_start_end_transitions = include_start_end_transitions
        self.crf = ConditionalRandomField(
            self.num_tags, constraints, include_start_end_transitions=include_start_end_transitions
        )

        self.metrics = {
            "accuracy": CategoricalAccuracy(),
            "accuracy3": CategoricalAccuracy(top_k=3),
        }
        self.calculate_span_f1 = calculate_span_f1
        if calculate_span_f1:
            if not label_encoding:
                raise ConfigurationError(
                    "calculate_span_f1 is True, but " "no label_encoding was specified."
                )
            self._f1_metric = SpanBasedF1Measure(
                vocab, tag_namespace=label_namespace, label_encoding=label_encoding
            )

        check_dimensions_match(
            text_field_embedder.get_output_dim(),
            encoder.get_input_dim(),
            "text field embedding dim",
            "encoder input dim",
        )
        if feedforward is not None:
            check_dimensions_match(
                encoder.get_output_dim(),
                feedforward.get_input_dim(),
                "encoder output dim",
                "feedforward input dim",
            )
        self.index_dict = {"[pseudo1]":0, "[pseudo2]":1, "[pseudo3]":2, "[pseudo4]":3, "[pseudo5]":4, "[pseudo6]":5, "[pseudo7]":6, "[pseudo8]":7, "[pseudo9]":8}
        self.orthogonal_embedding_emb = torch.nn.init.orthogonal_(torch.empty(self.num_virtual_models, text_field_embedder.get_output_dim(),requires_grad=False)).float()
        self.orthogonal_embedding_hidden = torch.nn.init.orthogonal_(torch.empty(self.num_virtual_models, encoder.get_output_dim(),requires_grad=False)).float()
        
        self.vocab = vocab

        initializer(self)

    @overrides
    def forward(
        self,  # type: ignore
        tokens: Dict[str, torch.LongTensor],
        tags: torch.LongTensor = None,
        metadata: List[Dict[str, Any]] = None,
        **kwargs
    ) -> Dict[str, torch.Tensor]:

        """
        Parameters
        ----------
        tokens : ``Dict[str, torch.LongTensor]``, required
            The output of ``TextField.as_array()``, which should typically be passed directly to a
            ``TextFieldEmbedder``. This output is a dictionary mapping keys to ``TokenIndexer``
            tensors.  At its most basic, using a ``SingleIdTokenIndexer`` this is: ``{"tokens":
            Tensor(batch_size, num_tokens)}``. This dictionary will have the same keys as were used
            for the ``TokenIndexers`` when you created the ``TextField`` representing your
            sequence.  The dictionary is designed to be passed directly to a ``TextFieldEmbedder``,
            which knows how to combine different word representations into a single vector per
            token in your input.
        tags : ``torch.LongTensor``, optional (default = ``None``)
            A torch tensor representing the sequence of integer gold class labels of shape
            ``(batch_size, num_tokens)``.
        metadata : ``List[Dict[str, Any]]``, optional, (default = None)
            metadata containg the original words in the sentence to be tagged under a 'words' key.

        Returns
        -------
        An output dictionary consisting of:

        logits : ``torch.FloatTensor``
            The logits that are the output of the ``tag_projection_layer``
        mask : ``torch.LongTensor``
            The text field mask for the input tokens
        tags : ``List[List[int]]``
            The predicted tags using the Viterbi algorithm.
        loss : ``torch.FloatTensor``, optional
            A scalar loss to be optimised. Only computed if gold label ``tags`` are provided.
        """
        
        try:
            # print("tokens", tokens["tokens"])
            index = torch.tensor([self.index_dict[self.vocab.get_token_from_index(i)] for i in tokens["tokens"]["tokens"][:,0].squeeze(0).tolist()]).long().to(tokens["tokens"]["tokens"].device)
            # print(tokens["tokens"][:,0])
            # exit()
            # index = torch.tensor([self.index_dict[self.vocab.get_token_from_index(i)] for i in tokens["tokens"][:,0].squeeze(0).tolist()]).long().to(tokens["tokens"].device)
        except:
            import sys
            sys.stdout.write("AAA {}".format(sys.exc_info()))
            index = torch.tensor([self.index_dict[self.vocab.get_token_from_index(i)] for i in tokens["tokens"]["tokens"][:,0].tolist()]).long().to(tokens["tokens"]["tokens"].device)
        
        
        embedded_text_input = self.text_field_embedder(tokens)
        
        # # orthogonal embedding
        orthogonal_vecs = torch.index_select(self.orthogonal_embedding_emb.to(embedded_text_input.device),0,index).unsqueeze(1)
        bsz, seq_len, emb_dim = embedded_text_input.size()
        orthogonal_vecs = orthogonal_vecs.expand(-1, seq_len, -1)
        
        scale = 1

        # embedded_text_input = F.normalize(embedded_text_input,dim=-1)

        embedded_text_input += (scale*orthogonal_vecs)



        mask = util.get_text_field_mask(tokens)

        if self.dropout:
            embedded_text_input = self.dropout(embedded_text_input)


        encoded_text = self.encoder(embedded_text_input, mask)
    
        # # orthogonal embedding
        # orthogonal_vecs = torch.index_select(self.orthogonal_embedding_hidden.to(embedded_text_input.device),0,index).unsqueeze(1)
        # bsz, seq_len, emb_dim = encoded_text.size()
        # orthogonal_vecs = orthogonal_vecs.expand(-1, seq_len, -1)
        # encoded_text += orthogonal_vecs




        if self.dropout:
            encoded_text = self.dropout(encoded_text)

        if self._feedforward is not None:
            encoded_text = self._feedforward(encoded_text)
        
        # Truncate embedding coressponding to the pseudo labels
        encoded_text = encoded_text[:,1:,:]
        mask = mask[:,1:]
        tags = tags[:,1:]



        logits = self.tag_projection_layer(encoded_text)


        # ##voting###
        
        best_paths = self.crf.viterbi_tags(logits, mask)
        best_paths = list(map(lambda x:x[0],best_paths))

        if not self.encoder.training:
            ans = []
            for i in range(len(best_paths)):
                ans.append(voting(best_paths))
            best_paths = ans

            logits = torch.stack(([torch.sum(logits,0)/9 for i in range(bsz)]))
            mask = mask
                # mask = torch.stack(([mask for i in range(bsz)]),0)    
        
        predicted_tags = [x for x in best_paths]

        print(predicted_tags)
        print(tags)
        print()
    
        ## logits ###
        # # if not self.encoder.training:
        #     #print('logits',logits.shape)
        #     logits = torch.stack(([torch.sum(logits,0)/9 for i in range(bsz)]))
        #     mask = torch.stack(([mask for i in range(bsz)]),0)
        # # Just get the tags and ignore the score.
        # best_paths = self.crf.viterbi_tags(logits, mask)
        # predicted_tags = [x for x, y in best_paths]
   

        output = {"logits": logits, "mask": mask, "tags": predicted_tags}

        if tags is not None:
            # Add negative log-likelihood as loss
            log_likelihood = self.crf(logits, tags, mask)


            output["loss"] = -log_likelihood

            # Represent viterbi tags as "class probabilities" that we can
            # feed into the metrics
            class_probabilities = logits * 0.0


            for i, instance_tags in enumerate(predicted_tags):
                for j, tag_id in enumerate(instance_tags):
                    class_probabilities[i, j, tag_id] = 1


            for metric in self.metrics.values():
                metric(class_probabilities, tags, mask.float())

            if self.calculate_span_f1:
                self._f1_metric(class_probabilities, tags, mask.float())
        if metadata is not None:
            output["words"] = [x["words"] for x in metadata]

        return output

    # @overrides
    def decode(self, output_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        Converts the tag ids to the actual tags.
        ``output_dict["tags"]`` is a list of lists of tag_ids,
        so we use an ugly nested list comprehension.
        """
        output_dict["tags"] = [
            [
                self.vocab.get_token_from_index(tag, namespace=self.label_namespace)
                for tag in instance_tags
            ]
            for instance_tags in output_dict["tags"]
        ]

        return output_dict

    # @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        metrics_to_return = {
            metric_name: metric.get_metric(reset) for metric_name, metric in self.metrics.items()
        }

        if self.calculate_span_f1:
            f1_dict = self._f1_metric.get_metric(reset=reset)
            if self._verbose_metrics:
                metrics_to_return.update(f1_dict)
            else:
                metrics_to_return.update({x: y for x, y in f1_dict.items() if "overall" in x})
        return metrics_to_return