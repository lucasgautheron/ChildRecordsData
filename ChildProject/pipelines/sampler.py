from abc import ABC, abstractmethod
import argparse
import datetime
import multiprocessing as mp
import os
import pandas as pd

import ChildProject
from ChildProject.pipelines.pipeline import Pipeline

class Sampler(ABC):
    def __init__(self, project: ChildProject.projects.ChildProject):
        self.project = project
        self.segments = pd.DataFrame()
        self.annotation_set = ''
        self.target_speaker_type = []

    @abstractmethod
    def sample(self):
        pass

    @staticmethod
    def add_parser(parsers):
        pass

    def retrieve_segments(self):
        am = ChildProject.annotations.AnnotationManager(self.project)
        annotations = am.annotations
        annotations = annotations[annotations['set'] == self.annotation_set]
        self.segments = am.get_segments(annotations)

        if len(self.target_speaker_type):
            self.segments = self.segments[self.segments['speaker_type'].isin(self.target_speaker_type)]
        
    def assert_valid(self):
        require_columns = ['recording_filename', 'segment_onset', 'segment_offset']
        missing_columns = list(set(require_columns) - set(self.segments.columns))

        if missing_columns:
            raise Exception("custom segments are missing the following columns: {}".format(','.join(missing_columns)))        

class CustomSampler(Sampler):
    def __init__(self, project: ChildProject.projects.ChildProject, segments_path: str):
        super().__init__(project)
        self.segments_path = segments_path

    def sample(self: str):
        self.segments = pd.read_csv(self.segments_path)

        if 'time_seek' not in self.segments.columns:
                self.segments['time_seek'] = 0

        return self.segments

    @staticmethod
    def add_parser(samplers):
        parser = samplers.add_parser('custom', help = 'custom sampling')
        parser.add_argument('segments', help = 'path to selected segments datafame')

class RandomVocalizationSampler(Sampler):
    """Sample vocalizations based on some input annotation set.

    :param project: ChildProject instance of the target dataset.
    :type project: ChildProject.projects.ChildProject
    :param annotation_set: Set of annotations to get vocalizations from.
    :type annotation_set: str
    :param target_speaker_type: List of speaker types to sample vocalizations from.
    :type target_speaker_type: list
    :param sample_size: Amount of vocalizations to sample, per recording.
    :type sample_size: int
    """
    def __init__(self,
        project: ChildProject.projects.ChildProject,
        annotation_set: str,
        target_speaker_type: list,
        sample_size: int):

        super().__init__(project)
        self.annotation_set = annotation_set
        self.target_speaker_type = target_speaker_type
        self.sample_size = sample_size

    def sample(self):
        self.retrieve_segments()
        self.segments = self.segments.groupby('recording_filename').sample(self.sample_size)
        return self.segments

    @staticmethod
    def add_parser(samplers):
        parser = samplers.add_parser('random-vocalizations', help = 'random sampling')
        parser.add_argument('--annotation-set', help = 'annotation set', default = 'vtc')
        parser.add_argument('--target-speaker-type', help = 'speaker type to get chunks from', choices=['CHI', 'OCH', 'FEM', 'MAL'], nargs = '+', default = ['CHI'])
        parser.add_argument('--sample-size', help = 'how many samples per recording', required = True, type = int)

import numpy as np
from pydub import AudioSegment

class EnergyDetectionSampler(Sampler):
    """Sample windows within each recording, targetting those
    that have a signal energy higher than some threshold.

    :param project: ChildProject instance of the target dataset.
    :type project: ChildProject.projects.ChildProject
    :param windows_length: Length of each window, in seconds.
    :type windows_length: float
    :param windows_spacing: Spacing between the start of each window, in seconds.
    :type windows_spacing: float
    :param windows_count: How many windows to retain per recording.
    :type windows_count: int
    :param windows_offset: start of the first window, in seconds, defaults to 0
    :type windows_offset: float, optional
    :param threshold: lowest energy quantile to sample from, defaults to 0.8
    :type threshold: float, optional
    :param low_freq: if > 0, frequencies below will be filtered before calculating the energy, defaults to 0
    :type low_freq: int, optional
    :param high_freq: if < 100000, frequencies above will be filtered before calculating the energy, defaults to 100000
    :type high_freq: int, optional
    :param threads: amount of threads to run on, defaults to 1
    :type threads: int, optional
    """
    def __init__(self,
        project: ChildProject.projects.ChildProject,
        windows_length: float,
        windows_spacing: float,
        windows_count: int,
        windows_offset: float = 0,
        threshold: float = 0.8,
        low_freq: int = 0,
        high_freq: int = 100000,
        threads: int = 1,
        profile: str = ''
        ):

        super().__init__(project)
        self.windows_length = windows_length
        self.windows_count = windows_count
        self.windows_spacing = windows_spacing
        self.windows_offset = windows_offset
        self.threshold = threshold
        self.low_freq = low_freq
        self.high_freq = high_freq
        self.threads = threads
        self.profile = profile

    def compute_energy_loudness(self, chunk, sampling_frequency: int):
        if self.low_freq > 0 or self.high_freq < 100000:
            chunk_fft = np.fft.fft(chunk)
            freq = np.fft.fftfreq(len(chunk_fft), 1/sampling_frequency)
            chunk_fft = chunk_fft[(freq > self.low_freq) & (freq < self.high_freq)]
            return np.sum(np.abs(chunk_fft)**2)/len(chunk)
        else:
            return np.sum(chunk**2)

    def get_recording_windows(self, recording):
        if self.profile:
            recording_path = os.path.join(self.project.path, ChildProject.projects.ChildProject.CONVERTED_RECORDINGS, self.profile, recording['filename'])
        else:
            recording_path = os.path.join(self.project.path, ChildProject.projects.ChildProject.RAW_RECORDINGS, recording['filename'])

        audio = AudioSegment.from_wav(recording_path)
        duration = audio.duration_seconds
        channels = audio.channels
        frequency = int(audio.frame_rate)
        max_value = 256**(int(audio.sample_width))/2-1

        windows_starts = (1000*np.arange(self.windows_offset, duration - self.windows_length, self.windows_spacing)).astype(int)
        windows = []

        for start in windows_starts:
            energy = 0
            chunk = audio[start:start+int(1000*self.windows_length)].get_array_of_samples()
            
            for channel in range(channels):
                data = chunk[channel::channels]
                data = np.array([x/max_value for x in data])
                energy += self.compute_energy_loudness(data, frequency)

            windows.append({
                'segment_onset': start/1000,
                'segment_offset': start/1000+self.windows_length,
                'recording_filename': recording['filename'],
                'energy': energy
            })

        return pd.DataFrame(windows).set_index('recording_filename')
        

    def sample(self):
        pool = mp.Pool(processes = self.threads if self.threads >= 1 else mp.cpu_count())
        windows = pd.concat(pool.map(self.get_recording_windows, self.project.recordings.to_dict(orient = 'records')))
        windows = windows.merge(
            windows.groupby('recording_filename').agg(energy_threshold = ('energy', lambda a: np.quantile(a, self.threshold))),
            left_index = True,
            right_index = True
        )
        windows = windows[windows['energy'] >= windows['energy_threshold']]
        self.segments = windows.groupby('recording_filename').sample(self.windows_count)
        self.segments.reset_index(inplace = True)

    @staticmethod
    def add_parser(samplers):
        parser = samplers.add_parser('energy-detection', help = 'energy based activity detection')
        parser.add_argument('--windows-length', help = 'length of each window (in seconds)', required = True, type = float)
        parser.add_argument('--windows-spacing', help = 'spacing between the start of two consecutive windows (in seconds)', required = True, type = float)
        parser.add_argument('--windows-count', help = 'how many windows to sample from', required = True, type = int)
        parser.add_argument('--windows-offset', help = 'start of the first window (in seconds)', type = float, default = 0)
        parser.add_argument('--threshold', help = 'lowest energy quantile to sample from. default is 0.8 (i.e., sample from the 20%% windows with the highest energy).', default = 0.8, type = float)
        parser.add_argument('--low-freq', help = 'remove all frequencies below low-freq before calculating each window\'s energy. (in Hz)', default = 0, type = int)
        parser.add_argument('--high-freq', help = 'remove all frequencies above high-freq before calculating each window\'s energy. (in Hz)', default = 100000, type = int)
        parser.add_argument('--threads', help = 'amount of threads to run on', default = 1, type = int)
        parser.add_argument('--profile', help = 'name of the profile of recordings to use (uses raw recordings if empty)', default = '', type = str)



class HighVolubilitySampler(Sampler):
    def __init__(self,
        project: ChildProject.projects.ChildProject,
        annotation_set: str,
        target_speaker_type: list,
        windows_length: float,
        windows_count: int):

        super().__init__(project)
        self.annotation_set = annotation_set
        self.target_speaker_type = target_speaker_type
        self.windows_length = windows_length
        self.windows_count = windows_count

    def sample(self):
        self.retrieve_segments()
        return self.segments

    @staticmethod
    def add_parser(samplers):
        parser = samplers.add_parser('high-volubility', help = 'high-volubility targeted sampling')
        parser.add_argument('--annotation-set', help = 'annotation set', default = 'vtc')
        parser.add_argument('--target-speaker-type', help = 'speaker type to get chunks from', choices=['CHI', 'OCH', 'FEM', 'MAL'], nargs = '+', default = ['CHI'])
        parser.add_argument('--windows-length', help = 'window length (minutes)', required = True, type = float)
        parser.add_argument('--windows-count', help = 'how many windows to be sampled', required = True, type = int)

class SamplerPipeline(Pipeline):
    def __init__(self):
        self.segments = []

    def run(self, path, destination, sampler, func = None, **kwargs):
        parameters = locals()
        parameters = [[key, parameters[key]] for key in parameters if key not in ['self', 'kwargs']]
        parameters.extend([[key, kwargs[key]] for key in kwargs])

        self.project = ChildProject.projects.ChildProject(path)
        self.project.read()

        splr = None
        if sampler == 'random-vocalizations':
            splr = RandomVocalizationSampler(self.project, **kwargs)
        elif sampler == 'high-volubility':
            splr = HighVolubilitySampler(self.project, **kwargs)
        elif sampler == 'energy-detection':
            splr = EnergyDetectionSampler(self.project, **kwargs)

        if splr is None:
            raise Exception('invalid sampler')

        splr.sample()
        splr.assert_valid()
        self.segments = splr.segments

        if 'time_seek' in self.segments.columns:
            self.segments['segment_onset'] = self.segments['segment_onset'] + self.segments['time_seek']
            self.segments['segment_offset'] = self.segments['segment_offset'] + self.segments['time_seek']

        self.segments[self.segments.columns & {'recording_filename', 'segment_onset', 'segment_offset'}].to_csv(os.path.join(destination, 'segments.csv'), index = False)

        parameters.extend([
            ['version', ChildProject.__version__],
            ['date_sampled', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')]
        ])
        pd.DataFrame(
            data = parameters,
            columns = ['param', 'value']
        ).to_csv(os.path.join(destination, 'parameters.csv'), index = False)

    @staticmethod
    def setup_parser(parser):
        parser.add_argument('path', help = 'path to the dataset')
        samplers = parser.add_subparsers(help = 'sampler', dest = 'sampler')
        #CustomSampler.add_parser(samplers)
        RandomVocalizationSampler.add_parser(samplers)
        HighVolubilitySampler.add_parser(samplers)
        EnergyDetectionSampler.add_parser(samplers)

        parser.add_argument('destination', help = 'segments destination')