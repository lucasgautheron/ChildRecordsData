from abc import ABC, abstractmethod
import argparse
import datetime
import multiprocessing as mp
import numpy as np
import os
import sys
import pandas as pd
from pydub import AudioSegment
from yaml import dump

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

    def retrieve_segments(self, recording_filename = None):
        am = ChildProject.annotations.AnnotationManager(self.project)
        annotations = am.annotations
        annotations = annotations[annotations['set'] == self.annotation_set]

        if recording_filename:
            annotations = annotations[annotations['recording_filename'] == recording_filename]

        if annotations.shape[0] == 0:
            return None
        
        try:
            segments = am.get_segments(annotations)
        except:
            return None

        if len(self.target_speaker_type):
            segments = segments[segments['speaker_type'].isin(self.target_speaker_type)]

        return segments
        
    def assert_valid(self):
        require_columns = ['recording_filename', 'segment_onset', 'segment_offset']
        missing_columns = list(set(require_columns) - set(self.segments.columns))

        if missing_columns:
            raise Exception("custom segments are missing the following columns: {}".format(','.join(missing_columns)))

    def export_audio(self, destination, profile = None):
        self.assert_valid()

        for recording, segments in self.segments.groupby('recording_filename'):
            path = self.project.get_recording_path(recording, profile)
            
            audio = AudioSegment.from_file(path)

            for segment in segments.to_dict(orient = 'records'):
                output_name = "{}_{}_{}.{}".format(
                    os.path.splitext(recording)[0],
                    segment['segment_onset'],
                    segment['segment_offset'],
                    'wav'
                )
                output_path = os.path.join(destination, output_name)
                seg = audio[segment['segment_onset']:segment['segment_offset']]

                os.makedirs(os.path.dirname(output_path), exist_ok = True)
                seg.export(output_path, 'wav')

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

class PeriodicSampler(Sampler):
    """Periodic sampling of a recording.

    :param project: ChildProject instance of the target dataset.
    :type project: ChildProject.projects.ChildProject
    :param length: length of each segment, in milliseconds
    :type length: int
    :param period: spacing between two consecutive segments, in milliseconds
    :type period: int
    :param offset: offset of the first segment, in milliseconds, defaults to 0
    :type offset: int
    """
    def __init__(self,
        project: ChildProject.projects.ChildProject,
        length: int, period: int, offset: int = 0
        ):

        super().__init__(project)
        self.length = int(length)
        self.period = int(period)
        self.offset = int(offset)

    def sample(self):
        recordings = self.project.recordings
        
        if not 'duration' in recordings.columns:
            print("""recordings duration was not found in the metadata
            and an attempt will be made to calculate it.""")

            durations = self.project.compute_recordings_duration().dropna()
            recordings = recordings.merge(durations[durations['recording_filename'] != 'NA'], how = 'left', left_on = 'recording_filename', right_on = 'recording_filename')

        recordings['duration'].fillna(0, inplace = True)
        
        self.segments = recordings[['recording_filename', 'duration']].copy()
        self.segments['segment_onset'] = self.segments.apply(
            lambda row: np.arange(self.offset, row['duration']-self.length+1e-4, self.period+self.length),
            axis = 1
        )
        self.segments = self.segments.explode('segment_onset')
        self.segments['segment_onset'] = self.segments['segment_onset'].astype(int)
        self.segments['segment_offset'] = self.segments['segment_onset'] + self.length
        self.segments.rename(columns = {'recording_filename': 'recording_filename'}, inplace = True)

        return self.segments


    @staticmethod
    def add_parser(samplers):
        parser = samplers.add_parser('periodic', help = 'periodic sampling')
        parser.add_argument('--length', help = 'length of each segment, in milliseconds', type = float, required = True)
        parser.add_argument('--period', help = 'spacing between two consecutive segments, in milliseconds', type = float, required = True)
        parser.add_argument('--offset', help = 'offset of the first segment, in milliseconds', type = float, default = 0)

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
    :param threads: amount of threads to run on, defaults to 1
    :type threads: int, optional
    """
    def __init__(self,
        project: ChildProject.projects.ChildProject,
        annotation_set: str,
        target_speaker_type: list,
        sample_size: int,
        threads: int = 1):

        super().__init__(project)
        self.annotation_set = annotation_set
        self.target_speaker_type = target_speaker_type
        self.sample_size = sample_size
        self.threads = threads

    def _sample_recording(self, recording):
        segments = self.retrieve_segments(recording['recording_filename'])

        if segments is None:
            print("warning: no annotations from the set '{}' were found for the recording '{}'".format(
                self.annotation_set,
                recording['recording_filename']
            ))
            return pd.DataFrame(columns = ['segment_onset', 'segment_offset', 'recording_filename'])

        return segments.sample(frac = 1).head(self.sample_size)
        
    def sample(self):
        pool = mp.Pool(processes = self.threads if self.threads >= 1 else mp.cpu_count())
        self.segments = pool.map(self._sample_recording, self.project.recordings.to_dict(orient = 'records'))
        self.segments = pd.concat(self.segments)
        return self.segments

    @staticmethod
    def add_parser(samplers):
        parser = samplers.add_parser('random-vocalizations', help = 'random sampling')
        parser.add_argument('--annotation-set', help = 'annotation set', default = 'vtc')
        parser.add_argument('--target-speaker-type', help = 'speaker type to get chunks from', choices=['CHI', 'OCH', 'FEM', 'MAL'], nargs = '+', default = ['CHI'])
        parser.add_argument('--sample-size', help = 'how many samples per recording', required = True, type = int)
        parser.add_argument('--threads', help = 'amount of threads to run on', default = 1, type = int)


class EnergyDetectionSampler(Sampler):
    """Sample windows within each recording, targetting those
    that have a signal energy higher than some threshold.

    :param project: ChildProject instance of the target dataset.
    :type project: ChildProject.projects.ChildProject
    :param windows_length: Length of each window, in milliseconds.
    :type windows_length: int
    :param windows_spacing: Spacing between the start of each window, in milliseconds.
    :type windows_spacing: int
    :param windows_count: How many windows to retain per recording.
    :type windows_count: int
    :param windows_offset: start of the first window, in milliseconds, defaults to 0
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
        windows_length: int,
        windows_spacing: int,
        windows_count: int,
        windows_offset: int = 0,
        threshold: float = 0.8,
        low_freq: int = 0,
        high_freq: int = 100000,
        threads: int = 1,
        profile: str = ''
        ):

        super().__init__(project)
        self.windows_length = int(windows_length)
        self.windows_count = int(windows_count)
        self.windows_spacing = int(windows_spacing)
        self.windows_offset = int(windows_offset)
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
        recording_path = self.project.get_recording_path(recording['recording_filename'], self.profile)

        try:
            audio = AudioSegment.from_file(recording_path)
        except:
            print("failed to read '{}', is it a valid audio file ?".format(recording_path), file = sys.stderr)
            return pd.DataFrame()

        duration = int(audio.duration_seconds*1000)
        channels = audio.channels
        frequency = int(audio.frame_rate)
        max_value = 256**(int(audio.sample_width))/2-1

        windows_starts = np.arange(self.windows_offset, duration - self.windows_length, self.windows_spacing).astype(int)
        windows = []

        print("computing the energy of {} windows for recording {}...".format(len(windows_starts), recording['recording_filename']))
        for start in windows_starts:
            energy = 0
            chunk = audio[start:start+self.windows_length].get_array_of_samples()
            channel_energies = np.zeros(channels)

            for channel in range(channels):
                data = chunk[channel::channels]
                data = np.array([x/max_value for x in data])
                channel_energies[channel] = self.compute_energy_loudness(data, frequency)

            window = {
                'segment_onset': start,
                'segment_offset': start+self.windows_length,
                'recording_filename': recording['recording_filename'],
                'energy': np.sum(channel_energies)
            }
            window.update({
                'channel_{}'.format(channel): channel_energies[channel]
                for channel in range(channels)
            })
            windows.append(window)

        return pd.DataFrame(windows)
        

    def sample(self):
        recordings = self.project.recordings[self.project.recordings['recording_filename'] != 'NA']
        pool = mp.Pool(processes = self.threads if self.threads >= 1 else mp.cpu_count())
        windows = pd.concat(pool.map(self.get_recording_windows, recordings.to_dict(orient = 'records'))).set_index('recording_filename')
        windows = windows.merge(
            windows.groupby('recording_filename').agg(energy_threshold = ('energy', lambda a: np.quantile(a, self.threshold))),
            left_index = True,
            right_index = True
        )
        windows = windows[windows['energy'] >= windows['energy_threshold']]
        self.segments = windows.groupby('recording_filename').sample(frac = 1).head(self.windows_count)
        self.segments.reset_index(inplace = True)
        self.segments.drop_duplicates(['recording_filename', 'segment_onset', 'segment_offset'], inplace = True)

    @staticmethod
    def add_parser(samplers):
        parser = samplers.add_parser('energy-detection', help = 'energy based activity detection')
        parser.add_argument('--windows-length', help = 'length of each window (in milliseconds)', required = True, type = int)
        parser.add_argument('--windows-spacing', help = 'spacing between the start of two consecutive windows (in milliseconds)', required = True, type = int)
        parser.add_argument('--windows-count', help = 'how many windows to sample from', required = True, type = int)
        parser.add_argument('--windows-offset', help = 'start of the first window (in milliseconds)', type = int, default = 0)
        parser.add_argument('--threshold', help = 'lowest energy quantile to sample from. default is 0.8 (i.e., sample from the 20%% windows with the highest energy).', default = 0.8, type = float)
        parser.add_argument('--low-freq', help = 'remove all frequencies below low-freq before calculating each window\'s energy. (in Hz)', default = 0, type = int)
        parser.add_argument('--high-freq', help = 'remove all frequencies above high-freq before calculating each window\'s energy. (in Hz)', default = 100000, type = int)
        parser.add_argument('--threads', help = 'amount of threads to run on', default = 1, type = int)
        parser.add_argument('--profile', help = 'name of the profile of recordings to use (uses raw recordings if empty)', default = '', type = str)



class HighVolubilitySampler(Sampler):
    """Return the top ``windows_count`` windows (of length ``windows_length``)
    with the highest volubility from each recording,
    as calculated from the metric ``metric``.

    :param project: ChildProject instance of the target dataset.
    :type project: ChildProject.projects.ChildProject
    :param annotation_set: set of annotations to calculate volubility from.
    :type annotation_set: str
    :param metric: the metric to evaluate high-volubility. should be any of 'awc', 'ctc', 'cvc'.
    :type metric: str
    :param windows_length: length of the windows, in milliseconds
    :type windows_length: int
    :param windows_count: amount of top regions to extract per recording
    :type windows_count: int
    :param threads: amount of threads to run the sampler on
    :type threads: int
    """

    def __init__(self,
        project: ChildProject.projects.ChildProject,
        annotation_set: str,
        metric: str,
        windows_length: int,
        windows_count: int,
        threads: int = 1):

        super().__init__(project)
        self.annotation_set = annotation_set
        self.metric = metric
        self.windows_length = windows_length
        self.windows_count = windows_count
        self.threads = threads

    def _segment_scores(self, recording):
        segments = self.retrieve_segments(recording['recording_filename'])

        if segments is None:
            print("warning: no annotations from the set '{}' were found for the recording '{}'".format(
                self.annotation_set,
                recording['recording_filename']
            ))
            return pd.DataFrame(columns = ['segment_onset', 'segment_offset', 'recording_filename'])

        # NOTE: The timestamps were simply rounded to 5 minutes in the original 
        # code via a complicated string replacement , which imo was incorrect, but 
        # there are edge cases that must be decided (even though they are quiet small)
        segments['chunk'] = (segments['segment_offset']  // self.windows_length + 1).astype('int')

        segment_onsets = segments.groupby('chunk')['segment_onset'].min()
        segment_offsets = segments.groupby('chunk')['segment_offset'].max()

        # this dataframe contains the segment onset and offsets for the chunks we calculated.
        windows = pd.merge(segment_onsets, segment_offsets, left_index=True, right_index=True).reset_index()
        windows['recording_filename'] = recording['recording_filename']

        if self.metric == 'ctc':
            if 'lena_conv_turn_type' in segments.columns:
                # NOTE: This is the equivalent of CTC (tab1) in rlena_extract.R
                segments['is_CT'] = segments['lena_conv_turn_type'].isin(['TIFR', 'TIMR'])
            else:
                segments.sort_values(['segment_onset', 'segment_offset'])
                segments = segments[segments['speaker_type'].isin(['FEM', 'MAL', 'CHI'])]
                segments['duration'] = segments['segment_offset']-segments['segment_onset']
                segments['iti'] = segments['segment_onset'] - segments['segment_offset'].shift(1)
                segments['prev_speaker_type'] = segments['speaker_type'].shift(1)

                key_child_env = ['FEM', 'MAL', 'CHI']
                segments['is_CT'] = segments.apply(
                    lambda row: (row['iti'] < 1000) and (
                        (row['speaker_type'] == 'CHI' and row['prev_speaker_type'] in key_child_env) or
                        (row['speaker_type'] in key_child_env and row['prev_speaker_type'] == 'CHI')
                    ), axis = 1
                )

            segments = segments.groupby('chunk', as_index=False)[['is_CT']].sum().rename(columns={'is_CT': 'ctc'}).merge(windows)

        elif self.metric == 'cvc':
            # NOTE: This is the equivalent of CVC (tab2) in rlena_extract.R
            if 'utterances_count' in segments.columns:
                segments = segments[segments.speaker_type == 'CHI'].groupby('chunk', as_index=False)[['utterances_count']].sum().rename(columns={'utterances_count': 'cvc'}).merge(windows)
            else:
                segments = segments[segments.speaker_type == 'CHI'].groupby('chunk', as_index=False)[['segment_onset']].count().rename(columns={'segment_onset': 'cvc'}).merge(windows)
        
        elif self.metric == 'awc':
            # NOTE: This is the equivalent of AWC (tab3) in rlena_extract.R
            segments = segments[segments.speaker_type.isin(['FEM', 'MAL'])].groupby('chunk', as_index=False)[['words']].sum().rename(columns={'words': 'awc'}).merge(windows)
        
        else:
            raise ValueError("unknown metric '{}'".format(self.metric))

        return segments.sort_values(self.metric, ascending = False).head(self.windows_count).reset_index(drop = True)

    def sample(self):
        pool = mp.Pool(processes = self.threads if self.threads >= 1 else mp.cpu_count())
        self.segments = pool.map(self._segment_scores, self.project.recordings.to_dict(orient = 'records'))
        self.segments = pd.concat(self.segments)

    @staticmethod
    def add_parser(samplers):
        parser = samplers.add_parser('high-volubility', help = 'high-volubility targeted sampling')
        parser.add_argument('--annotation-set', help = 'annotation set', required = True)
        parser.add_argument('--metric', help = 'which metric should be used to evaluate volubility', required = True, choices = ['ctc', 'cvc', 'awc'])
        parser.add_argument('--windows-length', help = 'window length (milliseconds)', required = True, type = int)
        parser.add_argument('--windows-count', help = 'how many windows to be sampled', required = True, type = int)
        parser.add_argument('--threads', help = 'amount of threads to run on', default = 1, type = int)


class SamplerPipeline(Pipeline):
    def __init__(self):
        self.segments = []

    def run(self, path, destination, sampler, func = None, **kwargs):
        parameters = locals()
        parameters = [{key: parameters[key]} for key in parameters if key not in ['self', 'kwargs']]
        parameters.extend([{key: kwargs[key]} for key in kwargs])

        self.project = ChildProject.projects.ChildProject(path)
        self.project.read()

        splr = None
        if sampler == 'periodic':
            splr = PeriodicSampler(self.project, **kwargs)
        elif sampler == 'random-vocalizations':
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

        date = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

        os.makedirs(destination, exist_ok = True)
        segments_path = os.path.join(destination, 'segments_{}.csv'.format(date))
        parameters_path = os.path.join(destination, 'parameters_{}.yml'.format(date))

        self.segments[set(self.segments.columns) & {'recording_filename', 'segment_onset', 'segment_offset'}].to_csv(segments_path, index = False)
        print("exported sampled segments to {}".format(segments_path))
        dump({
            'parameters': parameters,
            'package_version': ChildProject.__version__,
            'date': date
        }, open(parameters_path, 'w+'))
        print("exported sampler parameters to {}".format(parameters_path))

        return segments_path

    @staticmethod
    def setup_parser(parser):
        parser.add_argument('path', help = 'path to the dataset')
        parser.add_argument('destination', help = 'segments destination')

        samplers = parser.add_subparsers(help = 'sampler', dest = 'sampler')
        PeriodicSampler.add_parser(samplers)
        RandomVocalizationSampler.add_parser(samplers)
        HighVolubilitySampler.add_parser(samplers)
        EnergyDetectionSampler.add_parser(samplers)

