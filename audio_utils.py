import json
import os
import shutil
import subprocess
import tempfile

import numpy as np
import requests

import audio_encoder.audio
from audio_encoder import inference as eif
from synthesizer import audio as sa, hparams as hp


FFMPEG_PATH = '/opt/lip2wav/ffmpeg-4.4.1-i686-static/ffmpeg'
RNNOISE_PATH = '/opt/rnnoise/examples/rnnoise_demo'

FFMPEG_OPTIONS = '-hide_banner -loglevel panic'

# normalise commands
NORMALISE_AUDIO_COMMAND = f'ffmpeg-normalize -f -q {{input_audio_path}} -o {{output_audio_path}} -ar 16000'
PAD_AUDIO_COMMAND = f'{FFMPEG_PATH} {FFMPEG_OPTIONS} -y -i {{input_audio_path}} -af "adelay={{delay}}000|{{delay}}000" {{output_audio_path}}'  # pads audio with delay seconds of silence
REMOVE_AUDIO_PAD_COMMAND = f'{FFMPEG_PATH} {FFMPEG_OPTIONS} -y -i {{input_audio_path}} -ss 00:00:{{delay}}.000 -acodec pcm_s16le {{output_audio_path}}'  # removes delay seconds of silence

# denoising commands
# NOTE: rnnoise change at line https://github.com/xiph/rnnoise/blob/1cbdbcf1283499bbb2230a6b0f126eb9b236defd/examples/rnnoise_demo.c#L53
# doesn't write first denoised bytes to output (0.02 seconds less of duration)
# the final output creates audio with duration 0.01 second < original (will have to do)
WAV_TO_PCM_COMMAND = f'{FFMPEG_PATH} {FFMPEG_OPTIONS} -y -i {{input_audio_path}} -ac 1 -ar 48000 -f s16le -acodec pcm_s16le {{output_audio_path}}'  # 16-bit pcm w/ 48khz
DENOISE_COMMAND = f'{RNNOISE_PATH} {{input_audio_path}} {{output_audio_path}}'
PCM_TO_WAV_COMMAND = f'{FFMPEG_PATH} {FFMPEG_OPTIONS} -y -f s16le -ar 48000 -ac 1 -i {{input_audio_path}} {{output_audio_path}}'
WAV_TO_16KHZ_COMMAND = f'{FFMPEG_PATH} {FFMPEG_OPTIONS} -y -i {{input_audio_path}} -ac 1 -vn -acodec pcm_s16le -ar 16000 {{output_audio_path}}'

# lrw cropping commands
CROP_AUDIO_COMMAND = f'{FFMPEG_PATH} {FFMPEG_OPTIONS} -y -i {{input_audio_path}} -ss 00:00:00.{{start_millis}} -t 00:00:00.{{duration_millis}} {{output_audio_path}}'

# apt-get install sox libsox-fmt-all
PLAY_SOUND_COMMAND = 'play {audio_path}'

SECS = 1
ENCODER_WEIGHTS = 'audio_encoder/saved_models/pretrained.pt'

eif.load_model(ENCODER_WEIGHTS, device='cpu')
os.environ['FFMPEG_PATH'] = FFMPEG_PATH  # required for rnnoise


def pad_audio(audio_file, delay):
    padded_audio_file = tempfile.NamedTemporaryFile(suffix='.wav')

    subprocess.call(PAD_AUDIO_COMMAND.format(
        input_audio_path=audio_file.name,
        delay=delay,
        output_audio_path=padded_audio_file.name
    ), shell=True)

    return padded_audio_file


def remove_audio_pad(audio_file, delay):
    stripped_audio_file = tempfile.NamedTemporaryFile(suffix='.wav')

    subprocess.call(REMOVE_AUDIO_PAD_COMMAND.format(
        input_audio_path=audio_file.name,
        delay=delay,
        output_audio_path=stripped_audio_file.name
    ), shell=True)

    return stripped_audio_file


def normalise_audio(audio_file):
    normalised_audio_file = tempfile.NamedTemporaryFile(suffix='.wav')

    subprocess.call(NORMALISE_AUDIO_COMMAND.format(
        input_audio_path=audio_file.name,
        output_audio_path=normalised_audio_file.name
    ), shell=True)

    return normalised_audio_file


def denoise_audio(audio_file):
    noisy_pcm_file = tempfile.NamedTemporaryFile(suffix='.pcm')
    denoised_pcm_file = tempfile.NamedTemporaryFile(suffix='.pcm')
    denoised_wav_file = tempfile.NamedTemporaryFile(suffix='.wav')
    denoised_wav_file_final = tempfile.NamedTemporaryFile(suffix='.wav')

    subprocess.call(WAV_TO_PCM_COMMAND.format(
        input_audio_path=audio_file.name,
        output_audio_path=noisy_pcm_file.name
    ), shell=True)

    subprocess.call(DENOISE_COMMAND.format(
        input_audio_path=noisy_pcm_file.name,
        output_audio_path=denoised_pcm_file.name
    ), shell=True)

    subprocess.call(PCM_TO_WAV_COMMAND.format(
        input_audio_path=denoised_pcm_file.name,
        output_audio_path=denoised_wav_file.name
    ), shell=True)

    subprocess.call(WAV_TO_16KHZ_COMMAND.format(
        input_audio_path=denoised_wav_file.name,
        output_audio_path=denoised_wav_file_final.name
    ), shell=True)

    for f in [noisy_pcm_file, denoised_pcm_file, denoised_wav_file]:
        f.close()

    return denoised_wav_file_final


def preprocess_audio(audio_file, delay=3):
    """
    It was found that denoising and then normalising the audio produced louder/more background noise
        - the denoising doesn't work as well on softer audio
        - then the normalising just makes the noise louder

    Normalising and then denoising the audio removed more noise but the sound was only slightly louder
        - normalising first makes the denoising process better
        - normalise again for good measure because the denoising process can make the speaking fainter

    Normalising requires audios >= 3 seconds, pad all with silence and remove after
    See: https://github.com/slhck/ffmpeg-normalize/issues/87
    """
    # pad, normalise, denoise, normalise and strip
    padded_audio_file = pad_audio(audio_file, delay=delay)
    normalised_1_audio_file = normalise_audio(padded_audio_file)
    denoised_audio_file = denoise_audio(normalised_1_audio_file)
    normalised_2_audio_file = normalise_audio(denoised_audio_file)
    stripped_audio_file = remove_audio_pad(normalised_2_audio_file, delay=delay)

    for f in [padded_audio_file, normalised_1_audio_file,
              denoised_audio_file, normalised_2_audio_file]:
        f.close()

    return stripped_audio_file


def crop_audio(audio_path, start, duration):
    output_audio_path = audio_path.replace('.wav', '_cropped.wav')

    subprocess.call(CROP_AUDIO_COMMAND.format(
        input_audio_path=audio_path,
        start_millis=start * 1000,
        duration_millis=duration * 1000,
        output_audio_path=output_audio_path
    ), shell=True)

    return output_audio_path


def extract_mel_spectrogram(audio_file):
    wav = sa.load_wav(audio_file.name, sr=hp.hparams.sample_rate)

    return sa.melspectrogram(wav, hp.hparams)


def get_audio_embeddings(audio_file):
    with open(audio_file.name, 'rb') as f:
        response = requests.post('http://127.0.0.1:6001/audio_embeddings', files={'audio': f.read()})
        if response.status_code != 200:
            print(response.content)
            return

        return json.loads(response.content)


def extract_audio_embeddings(audio_file, amount=5):
    # get speaker embedding for audio
    wav = audio_encoder.audio.preprocess_wav(audio_file.name)  # resamples the audio if required
    if len(wav) < SECS * audio_encoder.audio.sampling_rate:
        return []

    indices = np.random.choice(len(wav) - audio_encoder.audio.sampling_rate * SECS, amount)  # amount random numbers
    wavs = [wav[idx: idx + audio_encoder.audio.sampling_rate * SECS] for idx in indices]  # amount random sampled wavs
    embeddings = [eif.embed_utterance(wav).tolist() for wav in wavs]  # amount embeddings

    return embeddings


def play_audio(audio_file):
    subprocess.call(PLAY_SOUND_COMMAND.format(audio_path=audio_file.name),
                    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, shell=True)
