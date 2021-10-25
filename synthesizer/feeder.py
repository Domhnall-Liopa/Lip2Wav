from sklearn.model_selection import train_test_split
from synthesizer.utils.text import text_to_sequence
from synthesizer.infolog import log
import tensorflow as tf
import numpy as np
import threading
import time
import os
from os.path import dirname, join, basename, isfile
import cv2
import glob
import time


# _batches_per_group = 4
_batches_per_group = 2
train_feeding_status, test_feeding_status, load_next_test_sample = False, False, False

class Feeder:
	"""
		Feeds batches of data into queue on a background thread.
	"""

	def __init__(self, coordinator, hparams, num_test_batches):
		super(Feeder, self).__init__()
		self._coord = coordinator
		self._hparams = hparams
		self._train_offset = 0
		self._test_offset = 0
		self.num_test_batches = num_test_batches

		self.filelist = { 'train' : self._hparams.all_images, 'test' : self._hparams.all_test_images}

		self.test_steps = 2

		#pad input sequences with the <pad_token> 0 ( _ )
		self._pad = 0
		#explicitely setting the padding to a value that doesn"t originally exist in the spectogram
		#to avoid any possible conflicts, without affecting the output range of the model too much
		if hparams.symmetric_mels:
			self._target_pad = -hparams.max_abs_value
		else:
			self._target_pad = 0.
		#Mark finished sequences with 1s
		self._token_pad = 1.

		with tf.device("/cpu:0"):
			# Create placeholders for inputs and targets. Don"t specify batch size because we want
			# to be able to feed different batch sizes at eval time.
			self._placeholders = [
				tf.placeholder(tf.float32, shape=(None, hparams.T, hparams.img_size, 
													hparams.img_size, 3), name="inputs"),
				tf.placeholder(tf.int32, shape=(None,), name="input_lengths"),
				tf.placeholder(tf.float32, shape=(None, hparams.mel_step_size, hparams.num_mels), 
							   name="mel_targets"),
				#tf.placeholder(tf.float32, shape=(None, None), name="token_targets"),
				tf.placeholder(tf.int32, shape=(None, ), name="targets_lengths"),
				tf.placeholder(tf.int32, shape=(hparams.tacotron_num_gpus, None), 
							   name="split_infos"),
				
				# SV2TTS
				tf.placeholder(tf.float32, shape=(None, 256), 
							   name="speaker_embeddings")
			]

			# Create queue for buffering data
			#queue = tf.FIFOQueue(8, [tf.float32, tf.int32, tf.float32, tf.float32, 
			#						 tf.int32, tf.int32, tf.float32], name="input_queue")
			queue = tf.FIFOQueue(8, [tf.float32, tf.int32, tf.float32, 
									 tf.int32, tf.int32, tf.float32], name="input_queue")
			self._enqueue_op = queue.enqueue(self._placeholders)
			#self.inputs, self.input_lengths, self.mel_targets, self.token_targets, \
			#	self.targets_lengths, self.split_infos, self.speaker_embeddings = queue.dequeue()
			self.inputs, self.input_lengths, self.mel_targets, \
				self.targets_lengths, self.split_infos, self.speaker_embeddings = queue.dequeue()

			self.inputs.set_shape(self._placeholders[0].shape)
			self.input_lengths.set_shape(self._placeholders[1].shape)
			self.mel_targets.set_shape(self._placeholders[2].shape)
			#self.token_targets.set_shape(self._placeholders[3].shape)
			self.targets_lengths.set_shape(self._placeholders[3].shape)
			self.split_infos.set_shape(self._placeholders[4].shape)
			self.speaker_embeddings.set_shape(self._placeholders[5].shape)

			# Create eval queue for buffering eval data
			#eval_queue = tf.FIFOQueue(1, [tf.float32, tf.int32, tf.float32, tf.float32,  
			#							  tf.int32, tf.int32, tf.float32], name="eval_queue")
			eval_queue = tf.FIFOQueue(1, [tf.float32, tf.int32, tf.float32,  
										  tf.int32, tf.int32, tf.float32], name="eval_queue")
			self._eval_enqueue_op = eval_queue.enqueue(self._placeholders)
			#self.eval_inputs, self.eval_input_lengths, self.eval_mel_targets, \
			#	self.eval_token_targets, self.eval_targets_lengths, \
			#	self.eval_split_infos, self.eval_speaker_embeddings = eval_queue.dequeue()

			self.eval_inputs, self.eval_input_lengths, self.eval_mel_targets, \
				self.eval_targets_lengths, \
				self.eval_split_infos, self.eval_speaker_embeddings = eval_queue.dequeue()

			self.eval_inputs.set_shape(self._placeholders[0].shape)
			self.eval_input_lengths.set_shape(self._placeholders[1].shape)
			self.eval_mel_targets.set_shape(self._placeholders[2].shape)
			#self.eval_token_targets.set_shape(self._placeholders[3].shape)
			self.eval_targets_lengths.set_shape(self._placeholders[3].shape)
			self.eval_split_infos.set_shape(self._placeholders[4].shape)
			self.eval_speaker_embeddings.set_shape(self._placeholders[5].shape)

	def start_threads(self, session):
		self._session = session
		thread = threading.Thread(name="background", target=self._enqueue_next_train_group)
		thread.daemon = True #Thread will close when parent quits
		thread.start()

		test_thread = threading.Thread(name="background_test", target=self._enqueue_next_test_group)
		test_thread.daemon = True #Thread will close when parent quits
		test_thread.start()

	def _get_test_groups(self):
		# print('Getting test group')
		input_data, mel_target, embed_target = self.getitem(split='test')

		# embed_target = np.zeros([256], dtype=np.float32)
		return input_data, mel_target, embed_target, len(mel_target)

	def make_test_batches(self):
		start = time.time()

		batch_size = self._hparams.tacotron_batch_size
		outputs_per_step = self._hparams.outputs_per_step
		num_batches = self.num_test_batches

		# ensure no duplicate samples
		examples = []
		while len(examples) != (batch_size * num_batches):  # 512 samples
			example = self.get_test_item()
			if example in examples:
				continue
			examples.append(example)

		batches = [examples[i: i+batch_size] for i in range(0, len(examples), batch_size)]
		np.random.shuffle(batches)

		log('\nGenerated %d test batches of size %d in %.3f sec' % (len(batches), batch_size, time.time() - start))

		return batches, outputs_per_step

	def _enqueue_next_train_group(self):
		global train_feeding_status
		while not self._coord.should_stop():
			if train_feeding_status:
				start = time.time()
				# Read a group of examples
				n = self._hparams.tacotron_batch_size
				r = self._hparams.outputs_per_step
				examples = [self._get_next_example() for i in range(n * _batches_per_group)]
				# Bucket examples based on similar output sequence length for efficiency
				examples.sort(key=lambda x: x[-1])
				batches = [examples[i: i+n] for i in range(0, len(examples), n)]
				np.random.shuffle(batches)

				log("\nGenerated {} train batches of size {} in {:.3f} sec".format(len(batches), n, time.time() - start))
				for batch in batches:
					feed_dict = dict(zip(self._placeholders, self._prepare_batch(batch, r)))
					self._session.run(self._enqueue_op, feed_dict=feed_dict)
				train_feeding_status = False
			time.sleep(1)

	def get_data(self, img_name):
		window_fnames = self.get_window(img_name)
		if window_fnames is None:
			return None

		# mel = np.load(os.path.join(os.path.dirname(img_name), 'mels.npz'))['mel'].T
		mel = np.load(os.path.join(os.path.dirname(img_name), 'mels.npz'))['spec'].T
		mel = self.crop_audio_window(mel, img_name)
		if mel.shape[0] != self._hparams.mel_step_size:
			return None

		window = []
		for fname in window_fnames:
			img = cv2.imread(fname)
			try:
				img = cv2.resize(img, (self._hparams.img_size, self._hparams.img_size))
			except:
				continue

			window.append(img)
		x = np.asarray(window) / 255.

		## speaker embedding
		refs = np.load(os.path.join(os.path.dirname(img_name), 'ref.npz'))['ref']
		ref = refs[np.random.choice(len(refs))]  # can have multiple refs per speaker

		return x.astype(np.float32), mel.astype(np.float32), ref.astype(np.float32), len(mel)

	def _enqueue_next_test_group(self):
		global test_feeding_status, load_next_test_sample
		test_batches, r = self.make_test_batches()
		num_batches = len(test_batches)
		while not self._coord.should_stop():
			if test_feeding_status:  # begin testing time
				i = 0
				while i < num_batches:
					if load_next_test_sample:
						batch = test_batches[i]
						batch = [self.get_data(img_name) for img_name in batch]
						feed_dict = dict(zip(self._placeholders, self._prepare_batch(batch, r)))
						self._session.run(self._eval_enqueue_op, feed_dict=feed_dict)
						i += 1
						load_next_test_sample = False
					time.sleep(1)
				test_feeding_status = False
			time.sleep(1)

	def set_train_feeding_status(self, feeding_status):
		global train_feeding_status
		train_feeding_status = feeding_status

	def set_test_feeding_status(self, feeding_status):
		global test_feeding_status
		test_feeding_status = feeding_status

	def get_test_feeding_status(self):
		global test_feeding_status
		return test_feeding_status

	def set_load_next_test_sample(self, b):
		global load_next_test_sample
		load_next_test_sample = b

	def getitem(self, split='train'):
		while 1:
			idx = np.random.randint(len(self.filelist[split]))

			img_name = self.filelist[split][idx]

			if not os.path.isfile(os.path.join(os.path.dirname(img_name), 'mels.npz')):
				continue

			if not os.path.isfile(os.path.join(os.path.dirname(img_name), 'ref.npz')):
				continue

			window_fnames = self.get_window(img_name)
			if window_fnames is None:
				idx = np.random.randint(len(self.filelist[split]))
				continue

			# mel = np.load(os.path.join(os.path.dirname(img_name), 'mels.npz'))['mel'].T
			mel = np.load(os.path.join(os.path.dirname(img_name), 'mels.npz'))['spec'].T
			mel = self.crop_audio_window(mel, img_name)
			if (mel.shape[0] != self._hparams.mel_step_size):
				idx = np.random.randint(len(self.filelist[split]))
				continue
			break

		window = []
		for fname in window_fnames:
			img = cv2.imread(fname)
			try:
				img = cv2.resize(img, (self._hparams.img_size, self._hparams.img_size))
			except:
				continue

			window.append(img)
		x = np.asarray(window) / 255.

		## speaker embedding
		refs = np.load(os.path.join(os.path.dirname(img_name), 'ref.npz'))['ref']
		ref = refs[np.random.choice(len(refs))]  # can have multiple refs per speaker

		return x.astype(np.float32), mel.astype(np.float32), ref.astype(np.float32)

	def get_test_item(self):
		while 1:
			idx = np.random.randint(len(self.filelist['test']))
			img_name = self.filelist['test'][idx]

			if not os.path.isfile(os.path.join(os.path.dirname(img_name), 'mels.npz')):
				continue
			if not os.path.isfile(os.path.join(os.path.dirname(img_name), 'ref.npz')):
				continue

			window_fnames = self.get_window(img_name)
			if window_fnames is None:
				continue

			# mel = np.load(os.path.join(os.path.dirname(img_name), 'mels.npz'))['mel'].T
			mel = np.load(os.path.join(os.path.dirname(img_name), 'mels.npz'))['spec'].T
			mel = self.crop_audio_window(mel, img_name)
			if mel.shape[0] != self._hparams.mel_step_size:
				continue

			break

		return img_name

	def _get_next_example(self):
		"""Gets a single example (input, mel_target, token_target, linear_target, mel_length) from_ disk
		"""
		input_data, mel_target, embed_target = self.getitem()

		#return input_data, mel_target, token_target, embed_target, len(mel_target)
		return input_data, mel_target, embed_target, len(mel_target)
	
	def _prepare_batch(self, batches, outputs_per_step):
		assert 0 == len(batches) % self._hparams.tacotron_num_gpus
		size_per_device = int(len(batches) / self._hparams.tacotron_num_gpus)
		np.random.shuffle(batches)

		inputs = None
		mel_targets = None
		#token_targets = None
		targets_lengths = None
		split_infos = []

		targets_lengths = np.asarray([x[-1] for x in batches], dtype=np.int32) #Used to mask loss
		input_lengths = np.asarray([len(x[0]) for x in batches], dtype=np.int32)

		for i in range(self._hparams.tacotron_num_gpus):
			batch = batches[size_per_device*i:size_per_device*(i+1)]
			input_cur_device, input_max_len = self._prepare_inputs([x[0] for x in batch])
			inputs = np.concatenate((inputs, input_cur_device), axis=1) if inputs is not None else input_cur_device
			mel_target_cur_device, mel_target_max_len = self._prepare_targets([x[1] for x in batch], outputs_per_step)
			mel_targets = np.concatenate(( mel_targets, mel_target_cur_device), axis=1) if mel_targets is not None else mel_target_cur_device

			#Pad sequences with 1 to infer that the sequence is done
			#token_target_cur_device, token_target_max_len = self._prepare_token_targets([x[2] for x in batch], outputs_per_step)
			#token_targets = np.concatenate((token_targets, token_target_cur_device),axis=1) if token_targets is not None else token_target_cur_device
			split_infos.append([input_max_len, mel_target_max_len])

		split_infos = np.asarray(split_infos, dtype=np.int32)
		
		### SV2TTS ###
		
		#embed_targets = np.asarray([x[3] for x in batches])
		embed_targets = np.asarray([x[2] for x in batches])

		##############
		
		#return inputs, input_lengths, mel_targets, token_targets, targets_lengths, \
		#	   split_infos, embed_targets
		return inputs, input_lengths, mel_targets, targets_lengths, \
			   split_infos, embed_targets

	def _prepare_inputs(self, inputs):
		max_len = max([len(x) for x in inputs])
		return np.stack([self._pad_input(x, max_len) for x in inputs]), max_len

	def _prepare_targets(self, targets, alignment):
		max_len = max([len(t) for t in targets])
		data_len = self._round_up(max_len, alignment)
		return np.stack([self._pad_target(t, data_len) for t in targets]), data_len
	'''
	def _prepare_token_targets(self, targets, alignment):
		max_len = max([len(t) for t in targets]) + 1
		data_len = self._round_up(max_len, alignment)
		return np.stack([self._pad_token_target(t, data_len) for t in targets]), data_len
	'''
	def _pad_input(self, x, length):
		return np.pad(x, (0, length - x.shape[0]), mode="constant", constant_values=self._pad)

	def _pad_target(self, t, length):
		return np.pad(t, [(0, length - t.shape[0]), (0, 0)], mode="constant", constant_values=self._target_pad)

	def _pad_token_target(self, t, length):
		return np.pad(t, (0, length - t.shape[0]), mode="constant", constant_values=self._token_pad)

	def _round_up(self, x, multiple):
		remainder = x % multiple
		return x if remainder == 0 else x + multiple - remainder

	def _round_down(self, x, multiple):
		remainder = x % multiple
		return x if remainder == 0 else x - remainder


	####### MY FUNCTIONS##################
	def get_frame_id(self, frame):
		return int(basename(frame).split('.')[0])

	def get_window(self, center_frame):
		# TODO: What happens if center_id == 0 or len(frames)?
		#  if center_id = 0, window_ids = [-12 -> 12]
		#  if center_id = len(frames), window_ids = [len(frames) - 12 -> len(frames) + 12]
		#  both include window ids that don't exist as files
		#  this means both would be excluded from training which isn't what we want here
		#  especially when we need more

		# if self._hparams.T%2:
		# 	window_ids = range(center_id - self._hparams.T//2, center_id + self._hparams.T//2 + 1)
		# else:
		# 	window_ids = range(center_id - self._hparams.T//2, center_id + self._hparams.T//2)

		center_id = self.get_frame_id(center_frame)
		vidname = dirname(center_frame)
		num_timesteps_is_divisible = self._hparams.T % 2
		half_num_timesteps = self._hparams.T // 2
		num_video_frames = len(glob.glob(join(vidname, '*.jpg')))

		if center_id < half_num_timesteps:
			start = 0
			end = self._hparams.T
		elif center_id > (num_video_frames - half_num_timesteps):
			start = (num_video_frames - self._hparams.T) + 1
			end = num_video_frames + 1
		else:
			start = center_id - half_num_timesteps
			end =  center_id + half_num_timesteps + 1

		window_ids = range(start, end)

		window_fnames = []
		for frame_id in window_ids:
			frame = join(vidname, '{}.jpg'.format(frame_id))
			if not isfile(frame):
				return None
			window_fnames.append(frame)
		return window_fnames

	def crop_audio_window(self, spec, center_frame):
		# estimate total number of frames from spec (num_features, T)
		# num_frames = (T x hop_size * fps) / sample_rate
		start_frame_id = self.get_frame_id(center_frame) - self._hparams.T//2
		total_num_frames = int((spec.shape[0] * self._hparams.hop_size * self._hparams.fps) / self._hparams.sample_rate)

		start_idx = int(spec.shape[0] * start_frame_id / float(total_num_frames))
		end_idx = start_idx + self._hparams.mel_step_size
		return spec[start_idx : end_idx, :]

