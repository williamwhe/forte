# Adapted from https://github.com/luheng/lsgn/srl_eval_utils.py

# Evaluation util functions for PropBank SRL.

import codecs
import contextlib
import os
import subprocess
from collections import Counter
from typing import List, Dict, Optional, NamedTuple

from nlp.pipeline.models.srl.data import Span

_SRL_CONLL_EVAL_SCRIPT = "scripts/srl-eval.pl"


@contextlib.contextmanager
def shut_up(stderr: bool = True, stdout: bool = False):
    r"""Suppress output (probably generated by external script or badly-written
    libraries) for stderr or stdout.

    This method can be used as a decorator, or a context manager:

    .. code-block: python

        @shut_up(stderr=True)
        def verbose_func(...):
            ...

        with shut_up(stderr=True):
            ... # verbose stuff

    :param stderr: If ``True``, suppress output from stderr.
    :param stdout: If ``True``, suppress output from stdout.
    """
    # redirect output to /dev/null
    fds = ([1] if stdout else []) + ([2] if stderr else [])
    null_fds = [os.open(os.devnull, os.O_RDWR) for _ in fds]
    output_fds = [os.dup(fd) for fd in fds]
    for null_fd, fd in zip(null_fds, fds):
        os.dup2(null_fd, fd)
    yield
    # restore normal stderr
    for null_fd, output_fd, fd in zip(null_fds, output_fds, fds):
        os.dup2(output_fd, fd)
        os.close(null_fd)


def _print_f1(total_gold, total_predicted, total_matched, message=""):
    precision = (100.0 * total_matched / total_predicted
                 if total_predicted > 0 else 0)
    recall = 100.0 * total_matched / total_gold if total_gold > 0 else 0
    f1 = (2 * precision * recall / (precision + recall)
          if precision + recall > 0 else 0)
    print(f"{message}: Precision: {precision}, Recall: {recall}, F1: {f1}")
    return precision, recall, f1


def compute_span_f1(gold_data, predictions, task_name):
    assert len(gold_data) == len(predictions)
    total_gold = 0
    total_predicted = 0
    total_matched = 0
    total_unlabeled_matched = 0
    label_confusions = Counter()  # Counter of (gold, pred) label pairs.

    for i in range(len(gold_data)):
        gold = gold_data[i]
        pred = predictions[i]
        total_gold += len(gold)
        total_predicted += len(pred)
        for a0 in gold:
            for a1 in pred:
                if a0[0] == a1[0] and a0[1] == a1[1]:
                    total_unlabeled_matched += 1
                    label_confusions.update([(a0[2], a1[2]), ])
                    if a0[2] == a1[2]:
                        total_matched += 1
    prec, recall, f1 = _print_f1(
        total_gold, total_predicted, total_matched, task_name)
    ul_prec, ul_recall, ul_f1 = _print_f1(
        total_gold, total_predicted, total_unlabeled_matched,
        "Unlabeled " + task_name)
    return prec, recall, f1, ul_prec, ul_recall, ul_f1, label_confusions


class F1Result(NamedTuple):
    precision: float
    recall: float
    f1: float


def compute_srl_f1(sentences: List[List[str]],
                   gold_srl: List[Dict[int, List[Span]]],
                   predictions: List[Dict[int, List[Span]]],
                   srl_conll_eval_path: Optional[str] = None,
                   output_path: Optional[str] = None):
    r"""Compute F1 score for SRL evaluation.

    :param sentences: Sentences for each example. Each sentence is represented
        as a list of words.
    :param gold_srl: Gold SRL spans for each example. Gold spans for each
        example are represented as a dictionary, mapping predicate word
        positions to the list of spans with the word as predicate. Each span
        is a tuple of 3 elements: `(start, end, label)`.
    :param predictions: Predicted SRL spans for each example, in the same format
        as :attr:`gold_srl`.
    :param srl_conll_eval_path: Path to the gold labels in CoNLL format.

    :return: A tuple containing:

        - `result`: Unofficial results given by custom evaluation routines.
        - `conll_result`: Official results given by official evaluation script.
        - `ul_result`: Unofficial unlabeled results.
        - `label_confusions`: Confusion matrix of the labels.
        - `comp_sents`: Number of complete-match sentences.
    """
    assert len(gold_srl) == len(predictions)
    total_gold = 0
    total_predicted = 0
    total_matched = 0
    total_unlabeled_matched = 0
    comp_sents = 0
    label_confusions = Counter()

    # Compute unofficial F1 of SRL relations.
    for gold, prediction in zip(gold_srl, predictions):
        gold_rels = 0
        pred_rels = 0
        matched = 0
        for pred_id, gold_args in gold.items():
            filtered_gold_args = [a for a in gold_args
                                  if a[2] not in ["V", "C-V"]]
            total_gold += len(filtered_gold_args)
            gold_rels += len(filtered_gold_args)
            if pred_id not in prediction:
                continue
            for a0 in filtered_gold_args:
                for a1 in prediction[pred_id]:
                    if a0[0] == a1[0] and a0[1] == a1[1]:
                        total_unlabeled_matched += 1
                        label_confusions.update([(a0[2], a1[2]), ])
                        if a0[2] == a1[2]:
                            total_matched += 1
                            matched += 1
        for pred_id, args in prediction.items():
            filtered_args = [a for a in args if a[2] not in ["V"]]  # "C-V"]]
            total_predicted += len(filtered_args)
            pred_rels += len(filtered_args)

        if gold_rels == matched and pred_rels == matched:
            comp_sents += 1

    precision, recall, f1 = _print_f1(
        total_gold, total_predicted, total_matched, "SRL (unofficial)")
    ul_prec, ul_recall, ul_f1 = _print_f1(
        total_gold, total_predicted, total_unlabeled_matched,
        "Unlabeled SRL (unofficial)")

    if output_path is None:
        output_path = f'/tmp/srl_pred_{os.getpid():d}'

    # Prepare to compute official F1.
    if not srl_conll_eval_path:
        # print("No gold conll_eval data provided. Recreating ...")
        gold_path = output_path + '.gold'
        print_to_conll(sentences, gold_srl, gold_path, None)
        gold_predicates = None
    else:
        gold_path = srl_conll_eval_path
        gold_predicates = read_gold_predicates(gold_path)

    pred_output = output_path + '.pred'
    print_to_conll(sentences, predictions, pred_output, gold_predicates)

    # Evaluate twice with official script.
    with shut_up(stdout=True):
        child = subprocess.Popen(
            f'perl {_SRL_CONLL_EVAL_SCRIPT} {gold_path} {pred_output}',
            shell=True, stdout=subprocess.PIPE)
        eval_info = child.communicate()[0].decode('utf-8')
        child2 = subprocess.Popen(
            f'perl {_SRL_CONLL_EVAL_SCRIPT} {pred_output} {gold_path}',
            shell=True, stdout=subprocess.PIPE)
        eval_info2 = child2.communicate()[0].decode('utf-8')

    try:
        conll_recall = float(
            eval_info.strip().split("\n")[6].strip().split()[5])
        conll_precision = float(
            eval_info2.strip().split("\n")[6].strip().split()[5])
        if conll_recall + conll_precision > 0:
            conll_f1 = (2 * conll_recall * conll_precision /
                        (conll_recall + conll_precision))
        else:
            conll_f1 = 0
        # print(eval_info)
        # print(eval_info2)
        # print(f"Official CoNLL Precision={conll_precision}, "
        #       f"Recall={conll_recall}, Fscore={conll_f1}")
    except IndexError:
        conll_recall = 0
        conll_precision = 0
        conll_f1 = 0
        print("Unable to get FScore. Skipping.")

    return (F1Result(precision, recall, f1),
            F1Result(conll_precision, conll_recall, conll_f1),
            F1Result(ul_prec, ul_recall, ul_f1),
            label_confusions, comp_sents)


def print_sentence_to_conll(f, tokens, labels):
    """Print a labeled sentence into CoNLL format.
    """
    for label_column in labels:
        assert len(label_column) == len(tokens)
    for i in range(len(tokens)):
        f.write(tokens[i].ljust(15))
        for label_column in labels:
            f.write(label_column[i].rjust(15))
        f.write("\n")
    f.write("\n")


def read_gold_predicates(gold_path):
    with open(gold_path, "r") as f:
        gold_predicates = [[]]
        for line in f:
            line = line.strip()
            if not line:
                gold_predicates.append([])
            else:
                info = line.split()
                gold_predicates[-1].append(info[0])
    return gold_predicates


def print_to_conll(sentences, srl_labels, output_filename, gold_predicates):
    fout = codecs.open(output_filename, "w", "utf-8")
    for sent_id, words in enumerate(sentences):
        if gold_predicates is not None:
            assert len(gold_predicates[sent_id]) == len(words)
        pred_to_args = srl_labels[sent_id]
        props = ["-" for _ in words]
        col_labels = [["*" for _ in words] for _ in range(len(pred_to_args))]
        for i, pred_id in enumerate(sorted(pred_to_args.keys())):
            # To make sure CoNLL-eval script count matching predicates
            # as correct.
            if (gold_predicates is not None and
                    gold_predicates[sent_id][pred_id] != "-"):
                props[pred_id] = gold_predicates[sent_id][pred_id]
            else:
                props[pred_id] = "P" + words[pred_id]
            flags = [False] * len(words)
            for start, end, label in pred_to_args[pred_id]:
                if not max(flags[start:end + 1]):
                    col_labels[i][start] = "(" + label + col_labels[i][start]
                    col_labels[i][end] = col_labels[i][end] + ")"
                    for j in range(start, end + 1):
                        flags[j] = True
            # Add unpredicted verb (for predicted SRL).
            if not flags[pred_id]:
                col_labels[i][pred_id] = "(V*)"
        print_sentence_to_conll(fout, props, col_labels)
    fout.close()
