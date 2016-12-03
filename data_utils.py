# Copyright 2016 Louis Kirsch. All Rights Reserved.
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
# ==============================================================================
import fnmatch
import heapq
import os
import tarfile
import urllib.request

import librosa
import random
import numpy as np
import warnings
from array import array


class Vocabulary:
  """
  Maintains a vocabulary, i.e. a mapping from words to ids and vice-versa.
  """

  PAD_ID = 0
  GO_ID = 1
  EOS_ID = 2
  REPEAT_ID = 3
  SPACE_ID = 4

  SPECIAL_VOCABS_COUNT = 5

  A_ASCII_CODE = ord('a')

  SIZE = ord('z') - A_ASCII_CODE + 1 + SPECIAL_VOCABS_COUNT

  def letter_to_id(self, letter):
    if letter == ' ':
      return Vocabulary.SPACE_ID
    return ord(letter) - Vocabulary.A_ASCII_CODE + Vocabulary.SPECIAL_VOCABS_COUNT

  def id_to_letter(self, identifier):
    if identifier >= Vocabulary.SPECIAL_VOCABS_COUNT:
      return chr(identifier - Vocabulary.SPECIAL_VOCABS_COUNT + Vocabulary.A_ASCII_CODE)
    if identifier == Vocabulary.SPACE_ID:
      return ' '
    return ''

  def _sentence_to_ids_generator(self, sentence):
    prev_letter = None
    for letter in sentence:
      if letter == prev_letter:
        yield Vocabulary.REPEAT_ID
      else:
        yield self.letter_to_id(letter)
      prev_letter = letter

  def _ids_to_sentence_generator(self, identifiers):
    prev_letter = None
    for identifier in identifiers:
      if identifier != Vocabulary.REPEAT_ID:
        prev_letter = self.id_to_letter(identifier)
      yield prev_letter

  def sentence_to_ids(self, sentence):
    return list(self._sentence_to_ids_generator(sentence.lower()))

  def ids_to_sentence(self, identifiers):
    return ''.join(self._ids_to_sentence_generator(identifiers))


def calc_spectrogram(audio_data, samplerate, number_mels):
  S = librosa.feature.melspectrogram(audio_data, sr=samplerate, n_mels=number_mels)

  # Convert to log scale (dB). We'll use the peak power as reference.
  log_S = librosa.logamplitude(S, ref_power=np.max)
  return log_S.T


def iglob_recursive(directory, file_pattern):
  for root, dir_names, file_names in os.walk(directory):
    for filename in fnmatch.filter(file_names, file_pattern):
      yield os.path.join(root, filename)


class SpeechCorpusReader:
  """
  Reads and transforms the speech corpus to be used by the NN
  """
  def __init__(self, data_directory):
    self._data_directory = data_directory
    self._vocabulary = Vocabulary()
    self._transcript_dict = self._build_transcript()

  @staticmethod
  def _get_transcript_entries(transcript_directory):
    """
    Iterate over all transcript lines and yield splitted entries
    Args:
      transcript_files: transcript files to iterate over

    Returns: Iterator for all splitted entries

    """
    transcript_files = iglob_recursive(transcript_directory, '*.trans.txt')
    for transcript_file in transcript_files:
      with open(transcript_file, 'r') as f:
        for line in f:
          # Strip included new line symbol
          line = line.rstrip('\n')

          # Each line is in the form
          # 00-000000-0000 WORD1 WORD2 ...
          splitted = line.split(' ', 1)
          yield splitted

  def _build_transcript(self):
    """
    Builds a transcript from transcript files, mapping from audio-id to a list of vocabulary ids
    :return: the created transcript
    """

    # Create the transcript dictionary
    transcript_dict = dict()
    for splitted in self._get_transcript_entries(self._data_directory):
      transcript_dict[splitted[0]] = self._vocabulary.sentence_to_ids(splitted[1])

    return transcript_dict

  def generate_samples(self, directory, number_mels, infinite=True):
    """
    Generates samples from the given directory in random order
    :param directory: the sub-directory of the initial data directory to sample from
    :param fragment_length: the length of a input fragment in seconds
    :return: generator with (audio_fragments: ndarray, transcript: list(int)) tuples
    """
    audio_files = list(iglob_recursive(self._data_directory + '/' + directory, '*.flac'))
    # Infinite stream
    while True:
      random.shuffle(audio_files)
      for audio_file in audio_files:
        audio_data, samplerate = librosa.load(audio_file)
        audio_fragments = calc_spectrogram(audio_data, samplerate, number_mels)

        file_name = os.path.basename(audio_file)
        audio_id = os.path.splitext(file_name)[0]

        transcript = self._transcript_dict[audio_id]

        yield audio_fragments, transcript

      if not infinite:
        return


class BucketPicker:
  """
  Based on the read samples it fills and yields buckets
  """

  def __init__(self, sample_generator, bucket_sizes, threshold):
    self._sample_generator = sample_generator
    self._bucket_sizes = bucket_sizes
    self._buckets = [list() for i in range(len(bucket_sizes))]
    self._threshold = threshold

  def _find_matching_bucket(self, sample):
    sample_source_size = sample[0].shape[0]
    sample_target_size = len(sample[1])

    # Search for a bucket the sample fits in
    for bucket_id, (source_size, target_size) in enumerate(self._bucket_sizes):
      # FIXME why not <=, because of GO symbol?
      if sample_source_size < source_size and sample_target_size < target_size:
        bucket = self._buckets[bucket_id]
        return bucket, bucket_id
    warnings.warn('No bucket found for size source: {}, target: {}'.format(sample_source_size, sample_target_size))
    return None, -1

  def generate_buckets(self):
    """
    Generate buckets, always yielding the next bucket that exceeds threshold
    """
    for sample in self._sample_generator:
      bucket, bucket_id = self._find_matching_bucket(sample)
      if bucket is not None:
        bucket.append(sample)

        # Bucket is filled, let's yield it
        if len(bucket) >= self._threshold:
          yield bucket, self._bucket_sizes[bucket_id], bucket_id
          bucket.clear()

  def generate_all_buckets(self):
    """
    Yields full buckets of all types
    """
    for sample in self._sample_generator:
      bucket, bucket_id = self._find_matching_bucket(sample)
      if bucket is not None and len(bucket) < self._threshold:
        bucket.append(sample)

        # All buckets filled, let's yield them
        # FIXME what if a bucket is never filled?
        if all([len(b) >= self._threshold for b in self._buckets]):
          yield self._buckets
          for b in self._buckets:
            b.clear()


class SpeechCorpusProvider:
  """
  Ensures the availability and downloads the speech corpus if necessary
  """

  TRAIN_DIR = 'train'
  DEV_DIR = 'dev'
  TEST_DIR = 'test'

  DEV_CLEAN_SET = 'dev-clean'
  TRAIN_CLEAN_100_SET = 'train-clean-100'
  TRAIN_CLEAN_360_SET = 'train-clean-360'
  TEST_CLEAN_SET = 'test-clean'
  DATA_SETS = {
    (DEV_DIR, DEV_CLEAN_SET),
    (TRAIN_DIR, TRAIN_CLEAN_100_SET),
    (TRAIN_DIR, TRAIN_CLEAN_360_SET),
    (TEST_DIR, TEST_CLEAN_SET)
  }

  BASE_URL = 'http://www.openslr.org/resources/12/'
  SET_FILE_EXTENSION = '.tar.gz'
  TAR_ROOT = 'LibriSpeech/'

  def __init__(self, data_directory):
    self._data_directory = data_directory
    self._make_dir_if_not_exists(data_directory)
    self._make_dir_if_not_exists(os.path.join(
      data_directory, SpeechCorpusProvider.DEV_DIR))
    self._make_dir_if_not_exists(os.path.join(
      data_directory, SpeechCorpusProvider.TRAIN_DIR))

  def _make_dir_if_not_exists(self, directory):
    if not os.path.exists(directory):
      os.makedirs(directory)

  def _download_if_not_exists(self, remote_file_name):
    path = os.path.join(self._data_directory, remote_file_name)
    if not os.path.exists(path):
      print('Downloading {}...'.format(remote_file_name))
      urllib.request.urlretrieve(SpeechCorpusProvider.BASE_URL + remote_file_name, path)
    return path

  @staticmethod
  def _extract_from_to(tar_file_name, source, target_directory):
    print('Extracting {}...'.format(tar_file_name))
    with tarfile.open(tar_file_name, 'r:gz') as tar:
      source_members = [
        tarinfo for tarinfo in tar.getmembers()
        if tarinfo.name.startswith(SpeechCorpusProvider.TAR_ROOT + source)
      ]
      for member in source_members:
        # Extract without prefix
        member.name = member.name.replace(SpeechCorpusProvider.TAR_ROOT, '')
      tar.extractall(target_directory, source_members)

  def _is_ready(self, data_sets=DATA_SETS):
    data_set_paths = [os.path.join(set_type, set_name)
                      for set_type, set_name in data_sets]
    return all([os.path.exists(os.path.join(
      self._data_directory, data_set
    )) for data_set in data_set_paths])

  def _download(self, data_sets=DATA_SETS):
    for data_set_type, data_set_name in data_sets:
      remote_file = data_set_name + SpeechCorpusProvider.SET_FILE_EXTENSION
      self._download_if_not_exists(remote_file)

  def _extract(self, data_sets=DATA_SETS):
    for data_set_type, data_set_name in data_sets:
      local_file = os.path.join(
        self._data_directory, data_set_name + SpeechCorpusProvider.SET_FILE_EXTENSION)
      target_directory = os.path.join(self._data_directory, data_set_type)
      self._extract_from_to(local_file, data_set_name, target_directory)
    pass

  def ensure_availability(self, test_only=False):
    if test_only:
      data_sets = [(SpeechCorpusProvider.TEST_DIR, SpeechCorpusProvider.TEST_CLEAN_SET)]
    else:
      data_sets = SpeechCorpusProvider.DATA_SETS

    if not self._is_ready(data_sets):
      self._download(data_sets)
      self._extract(data_sets)
