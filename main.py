#! /usr/bin/python3
# -*- coding: utf-8 -*-
# Author: karljeon44
# Date: 7/9/23 10:11 AM
import argparse
import logging
import math
import os
import random
import shutil
import time
from pathlib import Path

import librosa
import numpy as np
import pyloudnorm as pyln
import soundfile as sf
import torch
import torchaudio
import tqdm

logger = logging.getLogger(__name__)

argparser = argparse.ArgumentParser('Beberry-Hidden-Singer Preliminary Preprocessing Argparser')
argparser.add_argument('-i', '--input', default='../DATA', help='path to input file or dir containing files to be preprocessed')
argparser.add_argument('-o', '--output', help='path to output dir (default: same dir as input)')
argparser.add_argument('-s', '--speaker', help='name/id of the speaker; not needed if the input dir has speaker id or name as sub-dir name')
argparser.add_argument('-sr', '--sample_rate', type=str.lower, default='44.1k', help='target sampling rate',
                       choices=['40', '40k', '40000', '44.1', '44.1k', '44100', '48', '48k', '48000'])
argparser.add_argument('-n', '--normalize', action='store_true', help='whether to apply loudness normalization (ITU-R BS.1770-4)')
argparser.add_argument('-p', '--peak', type=float, default=-.1, help='peak normalize audio to N dB')
argparser.add_argument('-l', '--loudness', type=float, default=-23.0, help='loudness normalize audio to N dB LUFS')
argparser.add_argument('-b', '--block_size', type=float, default=0.400, help='block size for loudness measurement')
argparser.add_argument('--slice', action='store_true', help='whether to slice input audio files into shorter slices')
argparser.add_argument('-abs_min', '--abs_min_duration', type=float, default=2.0, help='absolute minimum duration (in seconds) of audio slices to keep')
argparser.add_argument('-min', '--min_duration', type=float, default=5.0, help='minimum duration (in seconds) of audio slices')
argparser.add_argument('-max', '--max_duration', type=float, default=15.0, help='maximum duration (in seconds) of audio slices')
argparser.add_argument('--augment', action='store_true', help='whether to augment this dataset with fixed pitch shifting')
argparser.add_argument('--overwrite', action='store_true', help='whether to overwrite output dir if it already exists')


def slice_by_max_duration(gen: np.ndarray, slice_max_duration: float, rate: int):
  """Slice audio by max duration

  Args:
      gen: audio data, in shape (samples, channels)
      slice_max_duration: maximum duration of each slice
      rate: sample rate

  Returns:
      generator of sliced audio data
  """

  if len(gen) > slice_max_duration * rate:
    # Evenly split _gen into multiple slices
    n_chunks = math.ceil(len(gen) / (slice_max_duration * rate))
    chunk_size = math.ceil(len(gen) / n_chunks)

    for i in range(0, len(gen), chunk_size):
      yield gen[i: i + chunk_size]
  else:
    yield gen


class Slicer:
  def __init__(
          self,
          sr: int,
          threshold: float = -40.0,
          min_length: int = 5000,
          min_interval: int = 300,
          hop_size: int = 10,
          max_sil_kept: int = 5000,
  ):
    if not min_length >= min_interval >= hop_size:
      raise ValueError(
        "The following condition must be satisfied: min_length >= min_interval >= hop_size"
      )

    if not max_sil_kept >= hop_size:
      raise ValueError(
        "The following condition must be satisfied: max_sil_kept >= hop_size"
      )

    min_interval = sr * min_interval / 1000
    self.threshold = 10 ** (threshold / 20.0)
    self.hop_size = round(sr * hop_size / 1000)
    self.win_size = min(round(min_interval), 4 * self.hop_size)
    self.min_length = round(sr * min_length / 1000 / self.hop_size)
    self.min_interval = round(min_interval / self.hop_size)
    self.max_sil_kept = round(sr * max_sil_kept / 1000 / self.hop_size)


  def _apply_slice(self, waveform, begin, end):
    if len(waveform.shape) > 1:
      return waveform[:, begin * self.hop_size: min(waveform.shape[1], end * self.hop_size)]
    else:
      return waveform[begin * self.hop_size: min(waveform.shape[0], end * self.hop_size)]


  def slice(self, waveform):
    if len(waveform.shape) > 1:
      samples = waveform.mean(axis=0)
    else:
      samples = waveform

    if samples.shape[0] <= self.min_length:
      return [waveform]

    rms_list = librosa.feature.rms(y=samples, frame_length=self.win_size, hop_length=self.hop_size).squeeze(0)
    sil_tags = []
    silence_start = None
    clip_start = 0

    for i, rms in enumerate(rms_list):
      # Keep looping while frame is silent.
      if rms < self.threshold:
        # Record start of silent frames.
        if silence_start is None:
          silence_start = i
        continue

      # Keep looping while frame is not silent and silence start has not been recorded.
      if silence_start is None:
        continue

      # Clear recorded silence start if interval is not enough or clip is too short
      is_leading_silence = silence_start == 0 and i > self.max_sil_kept
      need_slice_middle = i - silence_start >= self.min_interval and i - clip_start >= self.min_length

      if not is_leading_silence and not need_slice_middle:
        silence_start = None
        continue

      # Need slicing. Record the range of silent frames to be removed.
      if i - silence_start <= self.max_sil_kept:
        pos = rms_list[silence_start: i + 1].argmin() + silence_start

        if silence_start == 0:
          sil_tags.append((0, pos))
        else:
          sil_tags.append((pos, pos))

        clip_start = pos
      elif i - silence_start <= self.max_sil_kept * 2:
        pos = rms_list[i - self.max_sil_kept: silence_start + self.max_sil_kept + 1].argmin()
        pos += i - self.max_sil_kept
        pos_l = rms_list[silence_start: silence_start + self.max_sil_kept + 1].argmin() + silence_start
        pos_r = rms_list[i - self.max_sil_kept: i + 1].argmin() + i - self.max_sil_kept

        if silence_start == 0:
          sil_tags.append((0, pos_r))
          clip_start = pos_r
        else:
          sil_tags.append((min(pos_l, pos), max(pos_r, pos)))
          clip_start = max(pos_r, pos)
      else:
        pos_l = rms_list[silence_start: silence_start + self.max_sil_kept + 1].argmin() + silence_start
        pos_r = rms_list[i - self.max_sil_kept: i + 1].argmin() + i - self.max_sil_kept

        if silence_start == 0:
          sil_tags.append((0, pos_r))
        else:
          sil_tags.append((pos_l, pos_r))

        clip_start = pos_r
      silence_start = None

    # Deal with trailing silence.
    total_frames = rms_list.shape[0]
    if silence_start is not None and total_frames - silence_start >= self.min_interval:
      silence_end = min(total_frames, silence_start + self.max_sil_kept)
      pos = rms_list[silence_start: silence_end + 1].argmin() + silence_start
      sil_tags.append((pos, total_frames + 1))

    # Apply and return slices.
    if len(sil_tags) == 0:
      return [waveform]
    else:
      chunks = []

      if sil_tags[0][0] > 0:
        chunks.append(self._apply_slice(waveform, 0, sil_tags[0][0]))

      for i in range(len(sil_tags) - 1):
        chunks.append(self._apply_slice(waveform, sil_tags[i][1], sil_tags[i + 1][0]))

      if sil_tags[-1][1] < total_frames:
        chunks.append(self._apply_slice(waveform, sil_tags[-1][1], total_frames))

      return chunks


def slice_audio_v2(
        audio: np.ndarray,
        rate: int,
        min_duration: float = 5.0,
        max_duration: float = 30.0,
        min_silence_duration: float = 0.3,
        top_db: int = -40,
        hop_length: int = 10,
        max_silence_kept: float = 0.5,
):
  """Slice audio by silence

  Args:
      audio: audio data, in shape (samples, channels)
      rate: sample rate
      min_duration: minimum duration of each slice
      max_duration: maximum duration of each slice
      min_silence_duration: minimum duration of silence
      top_db: threshold to detect silence
      hop_length: hop length to detect silence
      max_silence_kept: maximum duration of silence to be kept

  Returns:
      Iterable of sliced audio
  """

  if len(audio) / rate < min_duration:
    yield from slice_by_max_duration(audio, max_duration, rate)
    return

  slicer = Slicer(
    sr=rate,
    threshold=top_db,
    min_length=min_duration * 1000,
    min_interval=min_silence_duration * 1000,
    hop_size=hop_length,
    max_sil_kept=max_silence_kept * 1000,
  )

  for chunk in slicer.slice(audio):
    yield from slice_by_max_duration(chunk, max_duration, rate)


def slice_audio_file_v2(
        input_file,
        output_dir=None,
        min_duration: float = 5.0,
        abs_min_duration: float = 2.0,
        max_duration: float = 30.0,
        min_silence_duration: float = 0.3,
        top_db: int = -45,
        hop_length: int = 10,
        max_silence_kept: float = 0.5,
) :
  """
  Slice audio by silence and save to output folder

  Args:
      input_file: input audio file
      output_dir: output folder
      min_duration: minimum duration of each slice
      max_duration: maximum duration of each slice
      min_silence_duration: minimum duration of silence
      top_db: threshold to detect silence
      hop_length: hop length to detect silence
      max_silence_kept: maximum duration of silence to be kept
  """
  if output_dir is None:
    output_dir = os.path.dirname(input_file)
  output_dir = Path(output_dir)
  output_dir.mkdir(parents=True, exist_ok=True)

  input_fname = os.path.splitext(os.path.basename(input_file))[0]
  audio, rate = librosa.load(input_file, sr=None, mono=True)
  for idx, sliced in enumerate(
          slice_audio_v2(
            audio,
            rate,
            min_duration=min_duration,
            max_duration=max_duration,
            min_silence_duration=min_silence_duration,
            top_db=top_db,
            hop_length=hop_length,
            max_silence_kept=max_silence_kept,
          )
  ):
    # only if length is at least absolute minimum
    duration = float(len(sliced)) / rate
    if duration >= abs_min_duration:
      sf.write(str(output_dir / f"{input_fname}_{idx:04d}.wav"), sliced, rate)


def main():
  # 0. setup
  begin = time.time()

  args = argparser.parse_args()
  if args.output is None:
    if os.path.isfile(args.input):
      args.output = os.path.dirname(args.input)
    else:
      args.output = args.input
    print("Output not provided, defaulting to input dir")
  else:
    if os.path.exists(args.output) and args.overwrite and Path(args.input) != Path(args.output):
      print(f"[!] Found an existing output dir (`{args.output}`), overwriting..")
      shutil.rmtree(args.output, ignore_errors=True)

  print("***** ARGS *****")
  for k,v in vars(args).items():
    print(f"{k:<15}: {v}")
  os.makedirs(args.output, exist_ok=True)

  # 1. collect input files
  assert os.path.exists(args.input), f'input path (`{args.input}`) cannot be found'

  print("1. Collecting input data..")
  data_by_speaker = dict()
  if os.path.isfile(args.input):
    # args.input, fname = os.path.split(args.input)
    assert args.speaker is not None
    data_by_speaker[args.speaker] = [args.input]
  else:
    # a) for any wavs immediately under `args.input` dir, map them to `args.speaker`
    # b) for any wavs under sub-dir, the name of this sub-dir becomes the id/name of the speaker for these wavs
    for file_or_dir in tqdm.tqdm(os.listdir(args.input)):
      fpath = os.path.join(args.input, file_or_dir)
      if file_or_dir.endswith('.wav'):
        assert args.speaker is not None, 'must provide speaker name'
        if args.speaker in data_by_speaker:
          data_by_speaker[args.speaker].append(fpath)
        else:
          data_by_speaker[args.speaker] = [fpath]

      elif os.path.isdir(fpath):
        assert file_or_dir not in data_by_speaker, f'duplicate speaker name: `{file_or_dir}`'
        data_by_speaker[file_or_dir] = cur_dict = []
        for fname in os.listdir(fpath):
          if not fname.endswith('wav'):
            continue
          cur_dict.append(os.path.join(fpath, fname))
  print(f"Found {len(data_by_speaker)} number of speaker(s)")
  # print(data_by_speaker)

  print("2. Begin preprocessing..")
  sample_rate = args.sample_rate
  if sample_rate.startswith('48'):
    sample_rate = 48000
  elif sample_rate.startswith('44'):
    sample_rate = 44100
  else:
    sample_rate = 40000

  for i, (speaker, fpaths) in enumerate(data_by_speaker.items()):
    speaker_dirpath = os.path.join(args.output, speaker)
    os.makedirs(speaker_dirpath, exist_ok=True)

    for wav_fpath in tqdm.tqdm(fpaths, desc=f'[{i+1}:{speaker}]'):
      # a) resample as mono
      audio, _ = librosa.load(wav_fpath, sr=sample_rate, mono=True)

      # b) (optional) loudness normalization
      if args.normalize:
        # peak normalize audio to [peak] dB
        audio = pyln.normalize.peak(audio, args.peak)

        # measure the loudness first
        meter = pyln.Meter(sample_rate, block_size=args.block_size)  # creates BS.1770 meter
        _loudness = meter.integrated_loudness(audio)

        audio = pyln.normalize.loudness(audio, _loudness, args.loudness)

      # export
      output_fpath = os.path.join(speaker_dirpath, os.path.basename(wav_fpath))
      sf.write(output_fpath, audio, sample_rate)
      output_fpaths = [output_fpath]

      # c) (optional) augmentation
      if args.augment:
        waveform_shift = torchaudio.functional.pitch_shift(torch.from_numpy(audio), sample_rate, n_steps=random.choice([-5.0, 5.0]))
        output_aug_fpath = output_fpath.replace('.wav', '_aug.wav')
        sf.write(output_aug_fpath, waveform_shift.numpy(), sample_rate)
        output_fpaths.append(output_aug_fpath)

      # d) (optional) slicing, the already exported files above will be removed after being sliced
      if args.slice:
        for output_fpath in output_fpaths:
          slice_audio_file_v2(
            input_file=output_fpath,
            min_duration=args.min_duration,
            abs_min_duration=args.abs_min_duration,
            max_duration=args.max_duration,
          )
          os.remove(output_fpath)

  duration = time.time() - begin
  print(f"Execution Time: {int(duration//60)}m {duration%60:.2f}s")


if __name__ == '__main__':
  main()
