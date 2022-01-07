'''
Lightweight parser for starfiles
'''

import numpy as np
import pandas as pd
from datetime import datetime as dt
import os

from . import mrc
from .mrc import LazyImage
from . import utils
log = utils.log

class Starfile():
    
    def __init__(self, headers, df):
        assert headers == list(df.columns), f'{headers} != {df.columns}'
        self.headers = headers
        self.df = df

    def __len__(self):
        return len(self.df)

    @classmethod
    def load(self, starfile, relion31=False):
        f = open(starfile,'r')
        # get to data block
        BLOCK = 'data_particles' if relion31 else 'data_'
        while 1:
            for line in f:
                if line.startswith(BLOCK):
                    break
            break
        # get to header loop
        while 1:
            for line in f:
                if line.startswith('loop_'):
                    break
            break
        # get list of column headers
        while 1:
            headers = []
            for line in f:
                if line.startswith('_'):
                    headers.append(line)
                else:
                    break
            break 
        # assume all subsequent lines until empty line is the body
        headers = [h.strip().split()[0] for h in headers]
        body = [line]
        for line in f:
            if line.strip() == '':
                break
            body.append(line)
        # put data into an array and instantiate as dataframe
        words = [l.strip().split() for l in body]
        words = np.array(words)
        assert words.ndim == 2, f"Uneven # columns detected in parsing {set([len(x) for x in words])}. Is this a RELION 3.1 starfile?" 
        assert words.shape[1] == len(headers), f"Error in parsing. Number of columns {words.shape[1]} != number of headers {len(headers)}" 
        data = {h:words[:,i] for i,h in enumerate(headers)}
        df = pd.DataFrame(data=data)
        return self(headers, df)

    def write(self, outstar):
        f = open(outstar,'w')
        f.write('# Created {}\n'.format(dt.now()))
        f.write('\n')
        f.write('data_\n\n')
        f.write('loop_\n')
        f.write('\n'.join(self.headers))
        f.write('\n')
        for i in self.df.index:
            # TODO: Assumes header and df ordering is consistent
            f.write(' '.join([str(v) for v in self.df.loc[i]]))
            f.write('\n')
        #f.write('\n'.join([' '.join(self.df.loc[i]) for i in range(len(self.df))]))

    def get_particles(self, datadir=None, lazy=True):
        '''
        Return particles of the starfile

        Input:
            datadir (str): Overwrite base directories of particle .mrcs
                Tries both substituting the base path and prepending to the path
            If lazy=True, returns list of LazyImage instances, else np.array
        '''
        particles = self.df['_rlnImageName']

        # format is index@path_to_mrc
        particles = [x.split('@') for x in particles]
        ind = [int(x[0])-1 for x in particles] # convert to 0-based indexing
        mrcs = [x[1] for x in particles]
        if datadir is not None:
            mrcs = prefix_paths(mrcs, datadir)
        for path in set(mrcs):
            assert os.path.exists(path), f'{path} not found'
        header = mrc.parse_header(mrcs[0])
        D = header.D # image size along one dimension in pixels
        dtype = header.dtype
        stride = dtype().itemsize*D*D
        dataset = [LazyImage(f, (D,D), dtype, 1024+ii*stride) for ii,f in zip(ind, mrcs)]
        if not lazy:
            dataset = np.array([x.get() for x in dataset])
        return dataset

def prefix_paths(mrcs, datadir):
    mrcs1 = ['{}/{}'.format(datadir, os.path.basename(x)) for x in mrcs]
    mrcs2 = ['{}/{}'.format(datadir, x) for x in mrcs]
    try:
        for path in set(mrcs1):
            assert os.path.exists(path)
        mrcs = mrcs1
    except:
        for path in set(mrcs2):
            assert os.path.exists(path), f'{path} not found'
        mrcs = mrcs2
    return mrcs

def csparc_get_particles(csfile, datadir=None, lazy=True):
    metadata = np.load(csfile)
    ind = metadata['blob/idx'] # 0-based indexing
    mrcs = metadata['blob/path'].astype(str).tolist()
    if datadir is not None:
        mrcs = prefix_paths(mrcs, datadir)
    for path in set(mrcs):
        assert os.path.exists(path), f'{path} not found'
    D = metadata[0]['blob/shape'][0]
    dtype = np.float32
    stride = np.float32().itemsize*D*D
    dataset = [LazyImage(f, (D,D), dtype, 1024+ii*stride) for ii,f in zip(ind, mrcs)]
    if not lazy:
        dataset = np.array([x.get() for x in dataset])
    return dataset


class TiltSeriesStarfile():
    '''
    Class to handle a star file generated by Warp when exporting subtomograms as a particleseries
    Therefore have strong prior for what starfile should look like
    '''
    def __init__(self, headers, df):
        assert headers == list(df.columns), f'{headers} != {df.columns}'
        self.headers = headers
        self.df = df

    def __len__(self):
        return len(self.df)

    @classmethod
    def load(self, starfile):
        with(open(starfile, 'r')) as f:
            # get to header loop (we know there is only one data block)
            while 1:
                for line in f:
                    if line.startswith('loop_'):
                        break
                break
            # get list of column headers
            while 1:
                headers = []
                for line in f:
                    if line.startswith('_'):
                        headers.append(line)
                    elif line.startswith('\n'):
                        pass
                    else:
                        break
                break
            # all subsequent lines should be the data body
            headers = [h.strip().split()[0] for h in headers]
            body = [line]
            for line in f:
                if line.strip() == '':
                    break
                body.append(line)
            # put data into an array and instantiate as dataframe
            words = [l.strip().split() for l in body]
            words = np.array(words)
            assert words.shape[1] == len(
                headers), f"Error in parsing. Number of columns {words.shape[1]} != number of headers {len(headers)}"
            data = {h: words[:, i] for i, h in enumerate(headers)}
            df = pd.DataFrame(data=data)
        return self(headers, df)

    def get_particles(self, datadir=None, lazy=False):
        '''
        Return particles of the starfile as (n_ptcls * n_tilts, D, D)

        Input:
            datadir (str): Overwrite base directories of particle .mrcs
                Tries both substituting the base path and prepending to the path
            If lazy=True, returns list of LazyImage instances, else np.array
        '''
        images = self.df['_rlnImageName']
        images = [x.split('@') for x in images] # format is index@path_to_mrc
        ind = [int(x[0])-1 for x in images] # convert to 0-based indexing of full dataset
        mrcs = [x[1] for x in images]
        if datadir is not None:
            mrcs = prefix_paths(mrcs, datadir)
        for path in set(mrcs):
            assert os.path.exists(path), f'{path} not found'
        header = mrc.parse_header(mrcs[0])
        D = header.D # image size along one dimension in pixels
        dtype = header.dtype
        stride = dtype().itemsize*D*D
        dataset = [LazyImage(f, (D,D), dtype, 1024+ii*stride) for ii,f in zip(ind, mrcs)]
        if not lazy: # not sure how to implement lazy loading yet
            dataset = np.array([x.get() for x in dataset])
        # dataset = dataset.reshape((len(unique_ptcls), ntilts, D, D))
        return dataset

    def get_tiltseries_shape(self):
        unique_ptcls = self.df['_rlnGroupName'].unique()
        ntilts = self.df['_rlnGroupName'].value_counts().unique()
        assert len(ntilts) == 1, 'All particles must have the same number of tilt images!'
        return len(unique_ptcls), int(ntilts)

    def get_tiltseries_pixelsize(self):
        pixel_size = float(self.df['_rlnDetectorPixelSize'][0]) # expects pixel size in A/px
        return pixel_size

    def get_tiltseries_voltage(self):
        voltage = int(float(self.df['_rlnVoltage'][0])) # expects voltage in kV
        return voltage

    def get_tiltseries_dose_per_A2_per_tilt(self, ntilts):
        # extract dose in e-/A2 from _rlnCtfBfactor column of Warp starfile (scaled by -4)
        # detects nonuniform dose, due to differential exposure during data collection or excluding tilts during processing
        dose_series = self.df['_rlnCtfBfactor'][0:ntilts].to_numpy(dtype=float)/-4
        constant_dose_series = np.linspace(dose_series[0], dose_series[-1], num=ntilts, endpoint=True)
        constant_dose_step = np.all(np.isclose(dose_series, constant_dose_series))
        if not constant_dose_step:
            log('Caution: non-uniform dose detected between each tilt image. Check whether this is expected!')
        return dose_series

    def get_tiltseries_cosine_weight(self, ntilts):
        # following relion1.4 convention, weighting each tilt by cos(tilt angle)
        # see: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4559595/
        cosine_weights = self.df['_rlnCtfScalefactor'][0:ntilts].to_numpy(dtype=float)
        return cosine_weights