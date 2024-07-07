'''
Lightweight parser for starfiles
'''

import numpy as np
import pandas as pd
from datetime import datetime as dt
import os
import matplotlib.pyplot as plt
from typing import TextIO

from tomodrgn import mrc, utils
from tomodrgn.mrc import LazyImage
log = utils.log
vlog = utils.vlog

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


def guess_dtypes(df):
    # guess numerics (obj --> float64, float32, int, or uint)
    df2 = df.apply(pd.to_numeric, errors='coerce', downcast='unsigned')

    # force downcast floats (float64 --> float32)
    df2[df2.select_dtypes(np.float64).columns] = df2.select_dtypes(np.float64).astype(np.float32)

    # assign remaining to strings (obj --> str)
    str_cols = df2.columns[df2.isna().any()]
    df2[str_cols] = df[str_cols].astype("string")

    return df2

class TiltSeriesStarfile():
    '''
    Class to handle a star file generated by Warp when exporting subtomograms as a particleseries
    Therefore have strong prior for what starfile should look like
    '''
    def __init__(self, headers, df):
        assert headers == list(df.columns), f'{headers} != {df.columns}'
        self.headers = headers
        self.df = df

        headers_rot = [
            '_rlnAngleRot',
            '_rlnAngleTilt',
            '_rlnAnglePsi'
        ]

        headers_trans = [
            '_rlnOriginX',
            '_rlnOriginY',
        ]

        headers_ctf = [
            '_rlnDetectorPixelSize',
            '_rlnDefocusU',
            '_rlnDefocusV',
            '_rlnDefocusAngle',
            '_rlnVoltage',
            '_rlnSphericalAberration',
            '_rlnAmplitudeContrast',
            '_rlnPhaseShift'
        ]

        header_uid = '_rlnGroupName'

        header_dose = '_tomodrgnTotalDose'
        self.df[header_dose] = self.df['_rlnCtfBfactor'].to_numpy(dtype=np.float32) / -4

        header_tilt = '_tomodrgnPseudoStageTilt'  # pseudo because arccos returns values in [0,pi] so lose +/- tilt information
        self.df[header_tilt] = np.arccos(self.df['_rlnCtfScalefactor'].to_numpy(dtype=np.float32))

        self.headers_ctf = headers_ctf
        self.headers_rot = headers_rot
        self.headers_trans = headers_trans
        self.header_uid = header_uid
        self.header_dose = header_dose
        self.header_tilt = header_tilt

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
        df = pd.DataFrame(words, columns=headers)
        df = guess_dtypes(df)  # guessing dtypes to float/int/str per column to reduce object memory utilization
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
        lazyparticles = [LazyImage(f, (D,D), dtype, 1024+ii*stride) for ii,f in zip(ind, mrcs)]
        if lazy:
            return lazyparticles
        else:
            # preallocating numpy array for in-place loading, fourier transform, fourier transform centering, etc
            particles = np.zeros((len(lazyparticles), D+1, D+1), dtype=np.float32)
            for i, img in enumerate(lazyparticles): particles[i,:-1,:-1] = img.get().astype(np.float32)
            return particles

    def get_particles_stack(self, datadir=None, lazy=False):
        '''
        Return particles of the starfile as (n_ptcls * n_tilts, D, D)

        Input:
            datadir (str): Overwrite base directories of particle .mrcs
                Tries both substituting the base path and prepending to the path
            If lazy=True, returns list of LazyImage instances, else np.array
        '''
        images = self.df['_rlnImageName']
        images = [x.split('@') for x in images] # format is index@path_to_mrc
        self.df['_rlnImageNameInd'] = [int(x[0])-1 for x in images] # convert to 0-based indexing of full dataset
        self.df['_rlnImageNameBase'] = [x[1] for x in images]

        mrcs = []
        ind = []
        # handle starfiles where .mrcs stacks are referenced non-contiguously
        for i, group in self.df.groupby((self.df['_rlnImageNameBase'].shift() != self.df['_rlnImageNameBase']).cumsum(), sort=False):
            # mrcs = [path1, path2, ...]
            mrcs.append(group['_rlnImageNameBase'].iloc[0])
            # ind = [ [0, 1, 2, ..., N], [0, 3, 4, ..., M], ..., ]
            ind.append(group['_rlnImageNameInd'].to_numpy())

        if datadir is not None:
            mrcs = prefix_paths(mrcs, datadir)
        for path in set(mrcs):
            assert os.path.exists(path), f'{path} not found'

        header = mrc.parse_header(mrcs[0])
        D = header.D # image size along one dimension in pixels
        dtype = header.dtype
        stride = dtype().itemsize*D*D
        if lazy:
            lazyparticles = [LazyImage(file, (D, D), dtype, 1024 + ind_img * stride)
                             for ind_stack, file in zip(ind, mrcs)
                             for ind_img in ind_stack]
            return lazyparticles
        else:
            # preallocating numpy array for in-place loading, fourier transform, fourier transform centering, etc
            particles = np.zeros((len(self.df), D+1, D+1), dtype=np.float32)
            offset = 0
            for ind_stack, file in zip(ind, mrcs):
                particles[offset:offset+len(ind_stack), :-1, :-1] = mrc.LazyImageStack(file, dtype, (D,D), ind_stack).get()
                offset += len(ind_stack)
            return particles

    def get_tiltseries_shape(self):
        unique_ptcls = self.df['_rlnGroupName'].unique()
        ntilts = self.df['_rlnGroupName'].value_counts().unique().to_numpy()
        assert len(ntilts) == 1, 'All particles must have the same number of tilt images!'
        return len(unique_ptcls), int(ntilts)

    def get_tiltseries_pixelsize(self):
        # expects pixel size in A/px
        if '_rlnPixelSize' in self.df.columns:
            pixel_size = float(self.df['_rlnPixelSize'].iloc[0])
        elif '_rlnDetectorPixelSize' in self.df.columns:
            pixel_size = float(self.df['_rlnDetectorPixelSize'].iloc[0])
        else:
            raise ValueError
        return pixel_size

    def get_tiltseries_voltage(self):
        voltage = int(float(self.df['_rlnVoltage'].iloc[0])) # expects voltage in kV
        return voltage

    def get_tiltseries_dose_per_A2_per_tilt(self, ntilts):
        # extract dose in e-/A2 from _rlnCtfBfactor column of Warp starfile (scaled by -4)
        # detects nonuniform dose, due to differential exposure during data collection or excluding tilts during processing
        dose_series = self.df['_rlnCtfBfactor'].iloc[0:ntilts].to_numpy(dtype=float)/-4
        constant_dose_series = np.linspace(dose_series[0], dose_series[-1], num=ntilts, endpoint=True)
        constant_dose_step = np.allclose(dose_series, constant_dose_series, atol=0.01)
        if not constant_dose_step:
            vlog('Caution: non-uniform dose detected between each tilt image. Check whether this is expected!')
        return dose_series

    def get_tiltseries_cosine_weight(self, ntilts):
        # following relion1.4 convention, weighting each tilt by cos(tilt angle)
        # see: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4559595/
        cosine_weights = self.df['_rlnCtfScalefactor'].iloc[0:ntilts].to_numpy(dtype=float)
        return cosine_weights

    def get_ptcl_img_indices(self):
        df_grouped = self.df.groupby('_rlnGroupName', sort=False)
        ptcls_to_imgs_ind = [df_grouped.get_group(ptcl).index.to_numpy() for ptcl in df_grouped.groups]
        return ptcls_to_imgs_ind

    def get_image_size(self, datadir=None):
        images = self.df['_rlnImageName']
        images = [x.split('@') for x in images]  # format is index@path_to_mrc
        mrcs = [x[1] for x in images]
        if datadir is not None:
            mrcs = prefix_paths(mrcs, datadir)
        for path in set(mrcs):
            assert os.path.exists(path), f'{path} not found'
        header = mrc.parse_header(mrcs[0])
        return header.D  # image size along one dimension in pixels

    def make_test_train_split(self,
                              fraction_train: float = 0.5,
                              use_first_ntilts: int = None,
                              summary_stats: bool = True):
        '''
        Create indices for tilt images assigned to training vs testing a tomoDRGN model

        Parameters
            fraction_train    # fraction of each particle's tilt images to put in train dataset
            use_first_ntilts  # if not None, sample from first n tilts in star file (before test/train split)
            summary_stats     # if True, print summary statistics of particle sampling for test/train

        Returns
            train_image_inds  # array of True/False tilt image indices (per tilt image) used in train dataset
            test_image_inds   # array of True/False tilt image indices (per tilt image) used in test dataset

        '''

        # check required inputs are present
        assert '_rlnGroupName' in self.df.columns
        df_grouped = self.df.groupby('_rlnGroupName', sort=False)
        assert 0 < fraction_train <= 1.0

        # find minimum number of tilts present for any particle
        mintilt_df = np.nan
        for _, group in df_grouped:
            mintilt_df = min(len(group), mintilt_df)

        # get indices associated with train and test
        inds_train = []
        inds_test = []

        for particle_id, group in df_grouped:
            inds_img = group.index.to_numpy()

            if use_first_ntilts is not None:
                assert len(inds_img) >= use_first_ntilts, f'Requested use_first_ntilts: {use_first_ntilts} larger than number of tilts: {len(inds_img)} for particle: {particle_id}'
                inds_img = inds_img[:use_first_ntilts]

            n_inds_train = np.rint(len(inds_img) * fraction_train).astype(int)

            inds_img_train = np.random.choice(inds_img, size=n_inds_train, replace=False)
            inds_img_train = np.sort(inds_img_train)
            inds_train.append(inds_img_train)

            inds_img_test = np.array(list(set(inds_img) - set(inds_img_train)))
            inds_test.append(inds_img_test)

        # provide summary statistics
        if summary_stats:
            log(f'    Number of tilts sampled by inds_train: {set([len(inds_img_train) for inds_img_train in inds_train])}')
            log(f'    Number of tilts sampled by inds_test: {set([len(inds_img_test) for inds_img_test in inds_test])}')

        # flatten indices
        inds_train = np.asarray([ind_img for inds_img_train in inds_train for ind_img in inds_img_train])
        inds_test = np.asarray([ind_img for inds_img_test in inds_test for ind_img in inds_img_test])

        # sanity check
        assert len(set(inds_train) & set(inds_test)) == 0, len(set(inds_train) & set(inds_test))
        if use_first_ntilts is None:
            assert len(set(inds_train) | set(inds_test)) == len(self.df), len(set(inds_train) | set(inds_test))

        return inds_train, inds_test

    def plot_particle_uid_ntilt_distribution(self, outdir):
        ptcls_to_imgs_ind = self.get_ptcl_img_indices()
        uids = self.df[self.header_uid].unique()
        n_tilts = np.asarray([len(ptcl_to_imgs_ind) for ptcl_to_imgs_ind in ptcls_to_imgs_ind])

        fig, axes = plt.subplots(2, 1)
        axes[0].plot(uids)
        axes[0].set_ylabel(self.header_uid)
        axes[1].plot(n_tilts)
        axes[1].set_xlabel('star file particle index')
        axes[1].set_ylabel('ntilts per particle')

        plt.tight_layout()
        plt.savefig(f'{outdir}/particle_uid_ntilt_distribution.png', dpi=200)
        plt.close()


def get_tiltseries_dose_per_A2_per_tilt(df, ntilts=None):
    # extract dose in e-/A2 from _rlnCtfBfactor column of Warp starfile (scaled by -4)
    # detects nonuniform dose, due to differential exposure during data collection or excluding tilts during processing
    if ntilts is not None:
        ntilts = len(df)
    dose_series = df['_rlnCtfBfactor'].iloc[0:ntilts].to_numpy(dtype=float)/-4
    constant_dose_series = np.linspace(dose_series[0], dose_series[-1], num=ntilts, endpoint=True)
    constant_dose_step = np.allclose(dose_series, constant_dose_series, atol=0.01)
    if not constant_dose_step:
        vlog('Caution: non-uniform dose detected between each tilt image. Check whether this is expected!')
    return dose_series


class GenericStarfile():
    '''
    Class to parse any star file with any number of blocks to a (dictionary of) pandas dataframes.
    Input            : path to star file
    Attributes:
        sourcefile   : absolute path to source data star file
        preambles    : list of lists containing text preceeding each block in starfile
        block_names  : list of names for each data block
        blocks       : dictionary of {block_name: data_block_as_pandas_df}
    Methods:
        load         : automatically called by __init__ to read all data from .star
        write        : writes all object data to `outstar` optionally with timestamp
    Notes:
        Stores data blocks not initiated with `loop_` keyword as a list in the `preambles` attribute
        Will ignore comments between `loop_` and beginning of data block; will not be preserved if using .write()
        Will raise a RuntimeError if a comment is found within a data block initiated with `loop`
    '''

    def __init__(self, starfile):
        self.sourcefile = os.path.abspath(starfile)
        preambles, blocks = self.skeletonize()
        self.preambles = preambles
        if len(blocks) > 0:
            blocks = self.load(blocks)
            self.block_names = list(blocks.keys())
        self.blocks = blocks

    def __len__(self):
        return len(self.block_names)

    def skeletonize(self) -> tuple[list[list[str]], dict[str, [list[str], int, int]]]:
        '''
        Parse star file for key data including preamble lines, header lines, and first and last row numbers associated with each data block. Does not load the entire file.
        :return: preambles: list (for each data block) of lists (each line preceeding column header lines and following data rows, as relevant)
        :return: blocks: dict mapping block names (e.g. `data_particles`) to a list of constituent column headers (e.g. `_rlnImageName), the first file line containing the data values of that block, and the last file line containing data values of that block
        '''

        def parse_preamble(filehandle: TextIO,
                           line_count: int) -> tuple[list[str], str | None, int]:
            '''
            Parse a star file preamble (the lines preceeding column header lines and following data rows, as relevant)
            :param filehandle: pre-existing file handle from which to read the star file
            :param line_count: the currently active line number in the star file
            :return: preamble: list of lines comprising the preamble section
            :return: block_name: the name of the data block following the preamble section, or None if no data block follows
            :return: line_count: the currently active line number in the star file after parsing the preamble
            '''
            # parse all lines preceeding column headers (including 'loop_')
            preamble = []
            while True:
                line = filehandle.readline()
                line_count += 1
                if not line:
                    # end of file detected
                    return preamble, None, line_count
                preamble.append(line.strip())
                if line.startswith('loop_'):
                    # entering loop
                    block_name = [line for line in preamble if line != ''][-2]
                    return preamble, block_name, line_count

        def parse_single_block(filehandle: TextIO,
                               line_count: int) -> tuple[list[str], int, int, bool]:
            '''
            Parse a single data block of a star file
            :param filehandle: pre-existing file handle from which to read the star file
            :param line_count: the currently active line number in the star file
            :return: header: list of lines comprising the column headers of the data block
            :return: block_start_line: the first file line containing the data values of the data block
            :return: line_count: the currently active line number in the star file after parsing the data block
            :return: end_of_file: boolean indicating whether the entire file ends immediately following the data block
            '''
            header = []
            block_start_line = line_count
            while True:
                # populate header
                line = filehandle.readline()
                line_count += 1
                if not line.strip():
                    # blank line between `loop_` and first header row
                    continue
                elif line.startswith('_'):
                    # column header
                    header.append(line)
                    continue
                elif line.startswith('#'):
                    # line is a comment, discarding for now
                    print(f'Found comment at STAR file line {line_count}, will not be preserved if writing star file later')
                    continue
                elif len(line.split()) == len([column for column in header if column.startswith('_')]):
                    # first data line
                    block_start_line = line_count
                    break
                else:
                    # unrecognized data block format
                    raise RuntimeError
            while True:
                # get length of data block
                line = filehandle.readline()
                line_count += 1
                if not line:
                    # end of file, therefore end of data block
                    return header, block_start_line, line_count, True
                if line.strip() == '':
                    # end of data block
                    return header, block_start_line, line_count, False

        preambles = []
        blocks = {}
        line_count = 0
        with(open(self.sourcefile, 'r')) as f:
            while True:
                # iterates once per preamble/header/block combination, ends when parse_preamble detects EOF
                preamble, block_name, line_count = parse_preamble(f, line_count)
                if preamble:
                    preambles.append(preamble)
                if block_name is None:
                    return preambles, blocks

                header, block_start_line, line_count, end_of_file = parse_single_block(f, line_count)
                blocks[block_name] = [header, block_start_line, line_count]

                if end_of_file:
                    return preambles, blocks

    def load(self,
             blocks: dict[str, [list[str], int, int]]) -> dict[str, pd.DataFrame]:
        '''
        Load each data block of a pre-skeletonized star file into a pandas dataframe
        :param blocks: dict mapping block names (e.g. `data_particles`) to a list of constituent column headers (e.g. `_rlnImageName), the first file line containing the data values of that block, and the last file line containing data values of that block
        :return: dict mapping block names (e.g. `data_particles`) to the corresponding data as a pandas dataframe
        '''

        def load_single_block(header: list[str],
                              block_start_line: int,
                              block_end_line: int) -> pd.DataFrame:
            '''
            Load a single data block of a pre-skeletonized star file into a pandas dataframe
            :param header: list of column headers (e.g. `_rlnImageName) of the data block
            :param block_start_line: the first file line containing the data values of the data block
            :param block_end_line: the last file line containing data values of the data block
            :return: pandas dataframe of the data block values
            '''
            columns = [line.split(' ')[0] for line in header if line.startswith('_')]

            # load the first 1 row to get dtypes of columns
            df = pd.read_csv(self.sourcefile,
                             sep='\s+',
                             header=None,
                             names=columns,
                             index_col=None,
                             skiprows=block_start_line - 1,
                             nrows=1,
                             low_memory=True,
                             engine='c',
                            )
            df_dtypes = {column: dtype for column, dtype in zip(df.columns.values.tolist(), df.dtypes.values.tolist())}

            # convert object dtype columns to string
            for column, dtype in df_dtypes.items():
                if dtype == 'object':
                    df_dtypes[column] = pd.StringDtype()

            # load the full dataframe with dtypes specified
            df = pd.read_csv(self.sourcefile,
                             sep='\s+',
                             header=None,
                             names=columns,
                             index_col=None,
                             skiprows=block_start_line - 1,
                             nrows=block_end_line - block_start_line,
                             low_memory=True,
                             engine='c',
                             dtype=df_dtypes,
                            )
            return df

        for block_name in blocks.keys():
            header, block_start_line, block_end_line = blocks[block_name]
            blocks[block_name] = load_single_block(header, block_start_line, block_end_line)
        return blocks

    def write(self,
              outstar: str,
              timestamp: bool = False) -> None:
        '''
        Write out the starfile dataframe(s) as a new file
        :param outstar: name of the output star file, optionally as absolute or relative path
        :param timestamp: whether to include the timestamp of file creation as a comment in the first line of the file
        :return: None
        '''

        def write_single_block(filehandle: TextIO,
                               block_name: str) -> None:
            '''
            Write a single star file block to a pre-existing file handle
            :param filehandle: pre-existing file handle to which to write this block's contents
            :param block_name: name of star file block to write (e.g. `data_`, `data_particles`)
            :return: None
            '''
            df = self.blocks[block_name]
            headers = [f'{header} #{i + 1}' for i, header in enumerate(df.columns.values.tolist())]
            filehandle.write('\n'.join(headers))
            filehandle.write('\n')
            df.to_csv(filehandle, index=False, header=False, mode='a', sep='\t')

        with open(outstar, 'w') as f:
            if timestamp:
                f.write('# Created {}\n'.format(dt.now()))

            for preamble, block_name in zip(self.preambles, self.block_names):
                for row in preamble:
                    f.write(row)
                    f.write('\n')
                write_single_block(f, block_name)
                f.write('\n')

        print(f'Wrote {os.path.abspath(outstar)}')

    def get_particles_stack(self,
                            particles_block_name: str,
                            particles_path_column: str,
                            datadir: str = None,
                            lazy: bool = False) -> np.ndarray | list[LazyImage]:
        '''
        Load particle images referenced by starfile
        :param particles_block_name: name of star file block containing particle path column (e.g. `data_`, `data_particles`)
        :param particles_path_column: name of star file column containing path to particle images .mrcs (e.g. `_rlnImageName`)
        :param datadir: absolute path to particle images .mrcs to override particles_path_column
        :param lazy: whether to load particle images now in memory (False) or later on-the-fly (True)
        :return: np.ndarray of shape (n_ptcls * n_tilts, D, D) or list of LazyImage objects of length (n_ptcls * n_tilts)
        '''

        images = self.blocks[particles_block_name][particles_path_column]
        images = [x.split('@') for x in images]  # assumed format is index@path_to_mrc
        self.blocks[particles_block_name]['_rlnImageNameInd'] = [int(x[0]) - 1 for x in images]  # convert to 0-based indexing of full dataset
        self.blocks[particles_block_name]['_rlnImageNameBase'] = [x[1] for x in images]

        mrcs = []
        ind = []
        # handle starfiles where .mrcs stacks are referenced non-contiguously
        for i, group in self.blocks[particles_block_name].groupby(
                (self.blocks[particles_block_name]['_rlnImageNameBase'].shift() != self.blocks[particles_block_name]['_rlnImageNameBase']).cumsum(), sort=False):
            # mrcs = [path1, path2, ...]
            mrcs.append(group['_rlnImageNameBase'].iloc[0])
            # ind = [ [0, 1, 2, ..., N], [0, 3, 4, ..., M], ..., ]
            ind.append(group['_rlnImageNameInd'].to_numpy())

        if datadir is not None:
            mrcs = prefix_paths(mrcs, datadir)
        for path in set(mrcs):
            assert os.path.exists(path), f'{path} not found'

        header = mrc.parse_header(mrcs[0])
        D = header.D  # image size along one dimension in pixels
        dtype = header.dtype
        stride = dtype().itemsize * D * D
        if lazy:
            lazyparticles = [LazyImage(file, (D, D), dtype, 1024 + ind_img * stride)
                             for ind_stack, file in zip(ind, mrcs)
                             for ind_img in ind_stack]
            return lazyparticles
        else:
            # preallocating numpy array for in-place loading, fourier transform, fourier transform centering, etc
            particles = np.zeros((len(self.blocks[particles_block_name]), D + 1, D + 1), dtype=np.float32)
            offset = 0
            for ind_stack, file in zip(ind, mrcs):
                particles[offset:offset + len(ind_stack), :-1, :-1] = mrc.LazyImageStack(file, dtype, (D, D), ind_stack).get()
                offset += len(ind_stack)
            return particles


class TiltSeriesStarfileCisTEM():
    '''
    Class to handle a tilt series particle series star file generated by cisTEM with a single data block
    '''

    def __init__(self, headers, df, mrc_path):
        assert headers == list(df.columns), f'{headers} != {df.columns}'
        self.headers = headers
        self.df = df

        headers_rot = [
            '_cisTEMAnglePhi',
            '_cisTEMAngleTheta',
            '_cisTEMAnglePsi'
        ]

        # TODO scale tranlations by pixel size and invert sign
        headers_trans = [
            '_cisTEMXShift',
            '_cisTEMYShift',
        ]

        headers_ctf = [
            '_cisTEMPixelSize',
            '_cisTEMDefocus1',
            '_cisTEMDefocus2',
            '_cisTEMDefocusAngle',
            '_cisTEMMicroscopeVoltagekV',
            '_cisTEMMicroscopeCsMM',
            '_cisTEMAmplitudeContrast',
            '_cisTEMPhaseShift',
        ]

        header_uid = '_cisTEMParticleGroup'

        header_dose = '_cisTEMTotalExposure'

        headers_ignored = [
            '_cisTEMBeamTiltGroup',
            '_cisTEMBeamTiltX',
            '_cisTEMBeamTiltY',
            '_cisTEMBest2DClass',
            '_cisTEMImageShiftX',
            '_cisTEMImageShiftY',
            '_cisTEMLogP',
            '_cisTEMOccupancy',
            '_cisTEMScore',
            '_cisTEMScoreChange',
            '_cisTEMSigma',
            '_cisTEMImageActivity',
            '_cisTEMStackFilename',
            '_cisTEMOriginalImageFilename',
            '_cisTEMReference3DFilename',
            '_cisTEMPositionInStack',
            '_cisTEMPreExposure',  # TODO consider using this column to calculate appropriate mid-exposure dose as original dose filtering would
        ]

        self.headers_ctf = headers_ctf
        self.headers_rot = headers_rot
        self.headers_trans = headers_trans
        self.header_uid = header_uid
        self.header_dose = header_dose
        self.header_tilt = None
        self.mrc_path = mrc_path

    def __len__(self):
        return len(self.df)

    @classmethod
    def load(self, star_path, mrc_path):
        with(open(star_path, 'r')) as f:
            rows_to_skip_for_df = 0
            headers = []
            while True:
                for line in f:
                    rows_to_skip_for_df += 1
                    if line.startswith('data_'):
                        # entering data block
                        continue
                    elif line.startswith('loop_'):
                        # entering loop block
                        continue
                    elif line.strip().startswith('#'):
                        # commented lines are ignored
                        continue
                    elif line.strip() == '':
                        # blank lines are ignored
                        continue
                    elif line.startswith('_'):
                        # headers are interpreted as immediately starting with an underscore
                        headers.append(line)
                        continue
                    else:
                        # current and all subsequent lines should be the data body
                        rows_to_skip_for_df -= 1
                        break
                break
        headers = [h.strip().split()[0] for h in headers]
        df = pd.read_csv(star_path,
                         skiprows=rows_to_skip_for_df,
                         header=None,
                         index_col=None,
                         sep='\s+',
                         names=headers)
        return self(headers, df, mrc_path)

    def convert_to_relion_conventions(self):
        # trans_warp = trans_cistem * -1 / pixel_size
        log('Updating poses (shifts) to match RELION convention')
        self.df[self.headers_trans] = self.df[self.headers_trans] * -1 / self.df[self.headers_ctf[0]].to_numpy(dtype=np.float32)[0]

    def get_ptcl_img_indices(self):
        '''
        Create an array of arrays: particle indices containing corresponding image indices

        CAUTION: filtering star file by particle inds should use this, not by _cisTEMParticleGroup values
        NOTE: _cisTEMParticleGroup starfile-ordered unique indices define particle index (zero-indexed)
        NOTE: _cisTEMPositionInStack is image index (typically non-continuous monotonic for cisTEM star files)
        NOTE: accessing rows of starfile for particle i should use: s.df.iloc[ptcl_to_imgs_ind[i]]
        '''
        df_grouped = self.df.groupby('_cisTEMParticleGroup', sort=False)
        ptcls_to_imgs_ind = [df_grouped.get_group(ptcl).index.to_numpy(dtype=int, copy=True) for ptcl in df_grouped.groups]
        ptcls_to_imgs_ind = np.asarray(ptcls_to_imgs_ind, dtype='object')
        return ptcls_to_imgs_ind

    def get_particles(self, datadir=None, lazy=False):
        '''
        Return particles of the starfile as (n_ptcls * n_tilts, D, D)
        Accesses disk once per star file row (image)

        Input:
            datadir (str): overwrite base directories of particle .mrcs
            If lazy=True, returns list of LazyImage instances, else np.array
        '''
        # process index values
        image_inds = self.df['_cisTEMPositionInStack'].to_numpy()  # 1-indexed df values
        image_inds -= 1  # convert to 0-indexed
        assert (np.min(image_inds) >= 0)

        # process mrc path
        assert os.path.exists(self.mrc_path)
        if datadir is not None:
            mrcs = prefix_paths(self.mrc_path, datadir)

        # prepare key params to load images
        header = mrc.parse_header(self.mrc_path)
        D = header.D  # image size along one dimension in pixels
        dtype = header.dtype
        stride = dtype().itemsize * D * D

        # load the particles
        lazyparticles = [LazyImage(self.mrc_path, (D, D), dtype, 1024 + ii * stride) for ii in image_inds]
        if lazy:
            return lazyparticles
        else:
            # preallocating numpy array for in-place loading, fourier transform, fourier transform centering, etc
            particles = np.empty((len(lazyparticles), D + 1, D + 1), dtype=np.float32)
            for i, img in enumerate(lazyparticles): particles[i, :-1, :-1] = img.get().astype(np.float32)
            return particles

    def get_particles_stack(self, datadir=None, lazy=False):
        '''
        Return particles of the starfile as (n_ptcls * n_tilts, D, D)
        Accesses disk once per same mrc file for all ind-specified images, thus more efficient than get_particles
        For cisTEM, we assume all images come from the same single mrc file

        Input:
            datadir (str): overwrite base directories of particle .mrcs
            If lazy=True, returns list of LazyImage instances, else np.array
        '''
        # process index values
        image_inds = self.df['_cisTEMPositionInStack'].to_numpy(copy=True)  # 1-indexed df values
        image_inds -= 1  # convert to 0-indexed
        assert (np.min(image_inds) >= 0)
        self.df['_tomodrgnImageNameInd'] = image_inds

        # process mrc path
        assert os.path.exists(self.mrc_path)
        self.df['_tomodrgnImageNameBase'] = self.mrc_path

        # prepare key params to load images
        header = mrc.parse_header(self.mrc_path)
        D = header.D  # image size along one dimension in pixels
        dtype = header.dtype
        stride = dtype().itemsize * D * D

        mrcs = []
        ind = []
        # handle starfiles where .mrcs stacks are referenced non-contiguously
        for i, group in self.df.groupby((self.df['_tomodrgnImageNameBase'].shift() != self.df['_tomodrgnImageNameBase']).cumsum(), sort=False):
            # mrcs = [path1, path2, ...]
            mrcs.append(group['_tomodrgnImageNameBase'].iloc[0])
            # ind = [ [0, 1, 2, ..., N], [0, 3, 4, ..., M], ..., ]
            ind.append(group['_tomodrgnImageNameInd'].to_numpy())

        if datadir is not None:
            mrcs = prefix_paths(mrcs, datadir)

        if lazy:
            lazyparticles = [LazyImage(file, (D, D), dtype, 1024 + ind_img * stride)
                             for ind_stack, file in zip(ind, mrcs)
                             for ind_img in ind_stack]
            return lazyparticles
        else:
            # preallocating numpy array for in-place loading, fourier transform, fourier transform centering, etc
            particles = np.empty((len(self.df), D + 1, D + 1), dtype=np.float32)
            offset = 0
            for ind_stack, file in zip(ind, mrcs):
                particles[offset:offset + len(ind_stack), :-1, :-1] = mrc.LazyImageStack(file, dtype, (D, D), ind_stack).get()
                offset += len(ind_stack)
            return particles

    def get_tiltseries_pixelsize(self):
        # expects pixel size in A/px
        pixel_size = float(self.df['_cisTEMPixelSize'].iloc[0])
        return pixel_size

    def get_tiltseries_voltage(self):
        # expects voltage in kV
        voltage = int(float(self.df['_cisTEMMicroscopeVoltagekV'].iloc[0]))
        return voltage

    def get_image_size(self, datadir=None):
        assert os.path.exists(self.mrc_path), f'{self.mrc_path} not found'
        header = mrc.parse_header(self.mrc_path)
        return header.D  # image size along one dimension in pixels

    def make_test_train_split(self,
                              fraction_train: float = 0.5,
                              use_first_ntilts: int = None,
                              summary_stats: bool = True):
        '''
        Create indices for tilt images assigned to training vs testing a tomoDRGN model

        Parameters
            fraction_train    # fraction of each particle's tilt images to put in train dataset
            use_first_ntilts  # if not None, sample from first n tilts in star file (before test/train split)
            summary_stats     # if True, print summary statistics of particle sampling for test/train

        Returns
            train_image_inds  # array of True/False tilt image indices (per tilt image) used in train dataset
            test_image_inds   # array of True/False tilt image indices (per tilt image) used in test dataset

        '''

        # check required inputs are present
        assert '_cisTEMParticleGroup' in self.df.columns
        df_grouped = self.df.groupby('_cisTEMParticleGroup', sort=False)
        assert 0 < fraction_train <= 1.0

        # find minimum number of tilts present for any particle
        mintilt_df = np.nan
        for _, group in df_grouped:
            mintilt_df = min(len(group), mintilt_df)

        # get indices associated with train and test
        inds_train = []
        inds_test = []

        for particle_id, group in df_grouped:
            inds_img = group.index.to_numpy()

            if use_first_ntilts is not None:
                assert len(inds_img) >= use_first_ntilts, f'Requested use_first_ntilts: {use_first_ntilts} larger than number of tilts: {len(inds_img)} for particle: {particle_id}'
                inds_img = inds_img[:use_first_ntilts]

            n_inds_train = np.rint(len(inds_img) * fraction_train).astype(int)

            inds_img_train = np.random.choice(inds_img, size=n_inds_train, replace=False)
            inds_img_train = np.sort(inds_img_train)
            inds_train.append(inds_img_train)

            inds_img_test = np.array(list(set(inds_img) - set(inds_img_train)))
            inds_test.append(inds_img_test)

        # provide summary statistics
        if summary_stats:
            log(f'    Number of tilts sampled by inds_train: {set([len(inds_img_train) for inds_img_train in inds_train])}')
            log(f'    Number of tilts sampled by inds_test: {set([len(inds_img_test) for inds_img_test in inds_test])}')

        # flatten indices
        inds_train = np.asarray([ind_img for inds_img_train in inds_train for ind_img in inds_img_train])
        inds_test = np.asarray([ind_img for inds_img_test in inds_test for ind_img in inds_img_test])

        # sanity check
        assert len(set(inds_train) & set(inds_test)) == 0, len(set(inds_train) & set(inds_test))
        if use_first_ntilts is None:
            assert len(set(inds_train) | set(inds_test)) == len(self.df), len(set(inds_train) | set(inds_test))

        return inds_train, inds_test

    def plot_particle_uid_ntilt_distribution(self, outdir):
        ptcls_to_imgs_ind = self.get_ptcl_img_indices()
        uids = self.df[self.header_uid].unique()
        n_tilts = np.asarray([len(ptcl_to_imgs_ind) for ptcl_to_imgs_ind in ptcls_to_imgs_ind])

        fig, axes = plt.subplots(2, 1)
        axes[0].plot(uids)
        axes[0].set_ylabel(self.header_uid)
        axes[1].plot(n_tilts)
        axes[1].set_xlabel('star file particle index')
        axes[1].set_ylabel('ntilts per particle')

        plt.tight_layout()
        plt.savefig(f'{outdir}/particle_uid_ntilt_distribution.png', dpi=200)
        plt.close()