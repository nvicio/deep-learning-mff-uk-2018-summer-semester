#!/usr/bin/env python3

import numpy as np
import tensorflow as tf

import morpho_dataset

class Network:
	def __init__(self, threads, seed=42):
		# Create an empty graph and a session
		graph = tf.Graph()
		graph.seed = seed
		self.session = tf.Session(graph = graph, config=tf.ConfigProto(inter_op_parallelism_threads=threads,
		                                                               intra_op_parallelism_threads=threads))

	def construct(self, args, source_chars, target_chars, bow, eow):
		with self.session.graph.as_default():
			if args.recodex:
				tf.get_variable_scope().set_initializer(tf.glorot_uniform_initializer(seed=42))

			# Inputs
			self.sentence_lens = tf.placeholder(tf.int32, [None], name="sentence_lens")
			self.source_ids = tf.placeholder(tf.int32, [None, None], name="source_ids")
			self.source_seqs = tf.placeholder(tf.int32, [None, None], name="source_seqs")
			self.source_seq_lens = tf.placeholder(tf.int32, [None], name="source_seq_lens")
			self.target_ids = tf.placeholder(tf.int32, [None, None], name="target_ids")
			self.target_seqs = tf.placeholder(tf.int32, [None, None], name="target_seqs")
			self.target_seq_lens = tf.placeholder(tf.int32, [None], name="target_seq_lens")

			# Append EOW after target_seqs
			target_seqs = tf.reverse_sequence(self.target_seqs, self.target_seq_lens, 1)
			target_seqs = tf.pad(target_seqs, [[0, 0], [1, 0]], constant_values=eow)
			target_seq_lens = self.target_seq_lens + 1
			target_seqs = tf.reverse_sequence(target_seqs, target_seq_lens, 1)

			# Encoder
			source_embeddings = tf.get_variable("source_embeddings", [source_chars, args.char_dim])
			source_embedded = tf.nn.embedding_lookup(source_embeddings, self.source_seqs)

			# TODO: Using a GRU with dimension args.rnn_dim, process the embedded self.source_seqs
			# using bidirectional RNN. Store the summed fwd and bwd outputs in `source_encoded`
			# and the summed fwd and bwd states into `source_states`.
			(output_fwd, output_bwd), (state_fwd, state_bwd) = tf.nn.bidirectional_dynamic_rnn(
				tf.nn.rnn_cell.GRUCell(args.rnn_dim), tf.nn.rnn_cell.GRUCell(args.rnn_dim),
				source_embedded, sequence_length=self.source_seq_lens, dtype=tf.float32)
			source_encoded = output_fwd + output_bwd
			source_states = state_fwd + state_bwd

			# Index the unique words using self.source_ids and self.target_ids.
			sentence_mask = tf.sequence_mask(self.sentence_lens)
			source_encoded = tf.boolean_mask(tf.nn.embedding_lookup(source_encoded, self.source_ids), sentence_mask)
			source_states = tf.boolean_mask(tf.nn.embedding_lookup(source_states, self.source_ids), sentence_mask)
			source_lens = tf.boolean_mask(tf.nn.embedding_lookup(self.source_seq_lens, self.source_ids), sentence_mask)

			target_seqs = tf.boolean_mask(tf.nn.embedding_lookup(target_seqs, self.target_ids), sentence_mask)
			target_lens = tf.boolean_mask(tf.nn.embedding_lookup(target_seq_lens, self.target_ids), sentence_mask)

			# Decoder
			target_embeddings = tf.get_variable("target_embeddings", [target_chars, args.char_dim])
			target_embedded = tf.nn.embedding_lookup(target_embeddings, target_seqs)

			decoder_cell = tf.nn.rnn_cell.GRUCell(args.rnn_dim)
			decoder_layer = tf.layers.Dense(target_chars)

			# Attention
			# TODO: Generate three fully connected layers without activations:
			# - `source_layer` with args.rnn_dim units
			# - `state_layer` with args.rnn_dim units
			# - `weight_layer` with 1 unit
			source_layer = tf.layers.Dense(args.rnn_dim)
			state_layer = tf.layers.Dense(args.rnn_dim)
			weight_layer = tf.layers.Dense(1)

			# Decoder training
			def with_attention(inputs, states):
				# Note: recall e_ij formula from lecture 9, slide 47
				# TODO: Project source_encoded using source_layer.
				# TODO: Change shape of states from [a, b] to [a, 1, b] and project it using state_layer.
				projected = source_layer(source_encoded) + tf.expand_dims(state_layer(states), axis=1)

				# Note: recall \alpha_ij formula from lecture 9, slide 47
				# TODO: Sum the two above projections, apply tf.tanh and project the result using weight_layer.
				# The result has shape [x, y, 1].
				# TODO: Apply tf.nn.softmax to the latest result, using axis corresponding to source characters.
				weights = tf.nn.softmax(weight_layer(tf.tanh(projected)), axis=1)

				# TODO: Multiply the source_encoded by the latest result, and sum the results with respect
				# to the axis corresponding to source characters. This is the final attention.
				attention = tf.reduce_sum(source_encoded * weights, axis=1)

				# TODO: Return concatenation of inputs and the computed attention.
				return tf.concat([inputs, attention], axis=1)

			class DecoderTraining(tf.contrib.seq2seq.Decoder):
				@property
				def batch_size(self): return tf.shape(source_states)[0]
				@property
				def output_dtype(self): return tf.float32
				@property
				def output_size(self): return target_chars

				def initialize(self, name=None):
					finished = 0 >= target_lens
					states = source_states
					# TODO: Call with_attention on the embedded BOW characters of shape [self.batch_size].
					# You can use tf.fill to generate BOWs of appropriate size.
					inputs = with_attention(tf.nn.embedding_lookup(target_embeddings, tf.fill([self.batch_size], bow)), states)
					return finished, inputs, states

				def step(self, time, inputs, states, name=None):
					outputs, states = decoder_cell(inputs, states)
					outputs = decoder_layer(outputs)
					# TODO: Next input is with_attention called on character embeddings
					# with index `time` in target_embedded.
					next_input = with_attention(target_embedded[:, time], states)
					finished = time + 1 >= target_lens
					return outputs, states, next_input, finished
			output_layer, _, _ = tf.contrib.seq2seq.dynamic_decode(DecoderTraining())
			self.predictions_training = tf.argmax(output_layer, axis=2, output_type=tf.int32)

			class DecoderPrediction(tf.contrib.seq2seq.Decoder):
				@property
				def batch_size(self): return tf.shape(source_states)[0]
				@property
				def output_dtype(self): return tf.int32
				@property
				def output_size(self): return 1

				def initialize(self, name=None):
					finished = tf.fill([self.batch_size], False)
					states = source_states
					# TODO: Call with_attention on the embedded BOW characters of shape [self.batch_size].
					# You can use tf.fill to generate BOWs of appropriate size.
					inputs = with_attention(tf.nn.embedding_lookup(target_embeddings, tf.fill([self.batch_size], bow)), states)
					return finished, inputs, states

				def step(self, time, inputs, states, name=None):
					outputs, states = decoder_cell(inputs, states)
					outputs = decoder_layer(outputs)
					outputs = tf.argmax(outputs, axis=1, output_type=tf.int32)
					# TODO: Embed `outputs` using target_embeddings and pass it to with_attention.
					next_input = with_attention(tf.nn.embedding_lookup(target_embeddings, outputs), states)
					finished = tf.equal(outputs, eow)
					return outputs, states, next_input, finished
			self.predictions, _, self.prediction_lens = tf.contrib.seq2seq.dynamic_decode(
				DecoderPrediction(), maximum_iterations=tf.reduce_max(source_lens) + 10)

			# Training
			weights = tf.sequence_mask(target_lens, dtype=tf.float32)
			loss = tf.losses.sparse_softmax_cross_entropy(target_seqs, output_layer, weights=weights)
			global_step = tf.train.create_global_step()
			self.training = tf.train.AdamOptimizer().minimize(loss, global_step=global_step, name="training")

			# Summaries
			accuracy_training = tf.reduce_all(tf.logical_or(
				tf.equal(self.predictions_training, target_seqs),
				tf.logical_not(tf.sequence_mask(target_lens))), axis=1)
			self.current_accuracy_training, self.update_accuracy_training = tf.metrics.mean(accuracy_training)

			minimum_length = tf.minimum(tf.shape(self.predictions)[1], tf.shape(target_seqs)[1])
			accuracy = tf.logical_and(
				tf.equal(self.prediction_lens, target_lens),
				tf.reduce_all(tf.logical_or(
					tf.equal(self.predictions[:, :minimum_length], target_seqs[:, :minimum_length]),
					tf.logical_not(tf.sequence_mask(target_lens, maxlen=minimum_length))), axis=1))
			self.current_accuracy, self.update_accuracy = tf.metrics.mean(accuracy)

			self.current_loss, self.update_loss = tf.metrics.mean(loss, weights=tf.reduce_sum(weights))
			self.reset_metrics = tf.variables_initializer(tf.get_collection(tf.GraphKeys.METRIC_VARIABLES))

			summary_writer = tf.contrib.summary.create_file_writer(args.logdir, flush_millis=10 * 1000)
			self.summaries = {}
			with summary_writer.as_default(), tf.contrib.summary.record_summaries_every_n_global_steps(10):
				self.summaries["train"] = [tf.contrib.summary.scalar("train/loss", self.update_loss),
				                           tf.contrib.summary.scalar("train/accuracy", self.update_accuracy_training)]
			with summary_writer.as_default(), tf.contrib.summary.always_record_summaries():
				for dataset in ["dev", "test"]:
					self.summaries[dataset] = [tf.contrib.summary.scalar(dataset + "/loss", self.current_loss),
					                           tf.contrib.summary.scalar(dataset + "/accuracy", self.current_accuracy)]

			# Initialize variables
			self.session.run(tf.global_variables_initializer())
			with summary_writer.as_default():
				tf.contrib.summary.initialize(session=self.session, graph=self.session.graph)

	def train_epoch(self, train, batch_size):
		import sys

		while not train.epoch_finished():
			sentence_lens, _, charseq_ids, charseqs, charseq_lens = train.next_batch(batch_size, including_charseqs=True)
			self.session.run(self.reset_metrics)
			predictions, _, _ = self.session.run(
				[self.predictions_training, self.training, self.summaries["train"]],
				{self.sentence_lens: sentence_lens,
				 self.source_ids: charseq_ids[train.FORMS], self.target_ids: charseq_ids[train.LEMMAS],
				 self.source_seqs: charseqs[train.FORMS], self.target_seqs: charseqs[train.LEMMAS],
				 self.source_seq_lens: charseq_lens[train.FORMS], self.target_seq_lens: charseq_lens[train.LEMMAS]})

			form, gold_lemma, system_lemma = "", "", ""
			for i in range(charseq_lens[train.FORMS][0]):
				form += train.factors[train.FORMS].alphabet[charseqs[train.FORMS][0][i]]
			for i in range(charseq_lens[train.LEMMAS][0]):
				gold_lemma += train.factors[train.LEMMAS].alphabet[charseqs[train.LEMMAS][0][i]]
				system_lemma += train.factors[train.LEMMAS].alphabet[predictions[0][i]]
			print("Gold form: {}, gold lemma: {}, predicted lemma: {}".format(form, gold_lemma, system_lemma), file=sys.stderr)

	def evaluate(self, dataset_name, dataset, batch_size):
		self.session.run(self.reset_metrics)
		while not dataset.epoch_finished():
			sentence_lens, _, charseq_ids, charseqs, charseq_lens = dataset.next_batch(batch_size, including_charseqs=True)
			self.session.run([self.update_accuracy, self.update_loss],
			                 {self.sentence_lens: sentence_lens,
			                  self.source_ids: charseq_ids[train.FORMS], self.target_ids: charseq_ids[train.LEMMAS],
			                  self.source_seqs: charseqs[train.FORMS], self.target_seqs: charseqs[train.LEMMAS],
			                  self.source_seq_lens: charseq_lens[train.FORMS], self.target_seq_lens: charseq_lens[train.LEMMAS]})
		return self.session.run([self.current_accuracy, self.summaries[dataset_name]])[0]


if __name__ == "__main__":
	import argparse
	import datetime
	import os
	import re

	# Fix random seed
	np.random.seed(42)

	# Parse arguments
	parser = argparse.ArgumentParser()
	parser.add_argument("--batch_size", default=10, type=int, help="Batch size.")
	parser.add_argument("--char_dim", default=64, type=int, help="Character embedding dimension.")
	parser.add_argument("--epochs", default=10, type=int, help="Number of epochs.")
	parser.add_argument("--recodex", default=False, action="store_true", help="ReCodEx mode.")
	parser.add_argument("--rnn_dim", default=64, type=int, help="Dimension of the encoder and the decoder.")
	parser.add_argument("--threads", default=1, type=int, help="Maximum number of threads to use.")
	args = parser.parse_args()

	# Create logdir name
	args.logdir = "logs/{}-{}-{}".format(
		os.path.basename(__file__),
		datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S"),
		",".join(("{}={}".format(re.sub("(.)[^_]*_?", r"\1", key), value) for key, value in sorted(vars(args).items())))
	)
	if not os.path.exists("logs"): os.mkdir("logs") # TF 1.6 will do this by itself

	# Load the data
	train = morpho_dataset.MorphoDataset("czech-cac-train.txt", max_sentences=5000)
	dev = morpho_dataset.MorphoDataset("czech-cac-dev.txt", train=train, shuffle_batches=False)

	# Construct the network
	network = Network(threads=args.threads)
	network.construct(args, len(train.factors[train.FORMS].alphabet), len(train.factors[train.LEMMAS].alphabet),
	                  train.factors[train.LEMMAS].alphabet_map["<bow>"], train.factors[train.LEMMAS].alphabet_map["<eow>"])

	# Train
	for i in range(args.epochs):
		network.train_epoch(train, args.batch_size)

		accuracy = network.evaluate("dev", dev, args.batch_size)
		print("{:.2f}".format(100 * accuracy))