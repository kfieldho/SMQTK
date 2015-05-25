import abc
import logging
import json
import math
import mimetypes
import multiprocessing
import multiprocessing.pool
import numpy
import os
import os.path as osp
import pyflann
import sklearn.cluster
import tempfile

import smqtk_config

from smqtk.content_description import ContentDescriptor
from smqtk.utils import safe_create_dir, SimpleTimer, video_utils
from smqtk.utils.string_utils import partition_string
from smqtk.utils.video_utils import get_metadata_info

from . import utils


# noinspection PyAbstractClass,PyPep8Naming
class ColorDescriptor_Base (ContentDescriptor):
    """
    Simple implementation of ColorDescriptor feature descriptor utility for
    feature generation over images and videos.

    This was started as an attempt at gaining a deeper understanding of what's
    going on with this feature descriptor's use and how it applied to later use
    in an indexer.

    Codebook generated via kmeans given a set of input data. FLANN index model
    used for quantization, buily using auto-tuning (picks the best indexing
    algorithm of linear, kdtree, kmeans, or combined), and using the Chi-Squared
    distance function.

    """

    # colorDescriptor executable that should be on the PATH
    PROC_COLORDESCRIPTOR = 'colorDescriptor'

    # Distance function to use in FLANN indexing. See FLANN documentation for
    # available distance function types (under the MATLAB section reference for
    # valid string identifiers)
    FLANN_DISTANCE_FUNCTION = 'chi_square'

    # Total number of descriptors to use from input data to generate codebook
    # model. Fewer than this may be used if the data set is small, but if it is
    # greater, we randomly sample down to this count (occurs on a per element
    # basis).
    CODEBOOK_DESCRIPTOR_LIMIT = 1000000.

    def __init__(self, model_directory, work_directory,
                 kmeans_k=1024, flann_target_precision=0.95,
                 flann_sample_fraction=0.75,
                 random_seed=None):
        """
        Initialize a new ColorDescriptor interface instance.

        :param model_directory: Path to the directory to store/read data model
            files on the local filesystem. Relative paths are treated relative
            to ``smqtk_config.DATA_DIR``.
        :type model_directory: str | unicode

        :param work_directory: Path to the directory in which to place
            temporary/working files. Relative paths are treated relative to
            ``smqtk_config.WORD_DIR``.
        :type work_directory: str | unicode

        :param kmeans_k: Centroids to generate. Default of 1024
        :type kmeans_k: int

        :param flann_target_precision: Target precision percent to tune index
            for. Default is 0.90 (90% accuracy). For some codebooks, if this is
            too close to 1.0, the FLANN library may non-deterministically
            overflow, causing an infinite loop requiring a SIGKILL to stop.
        :type flann_target_precision: float

        :param flann_sample_fraction: Fraction of input data to use for index
            auto tuning. Default is 0.75 (75%).
        :type flann_sample_fraction: float

        :param random_seed: Optional value to seed components requiring random
            operations.
        :type random_seed: None or int

        """
        # TODO: Because of the FLANN library non-deterministic overflow issue,
        #       an alternative must be found before this can be put into
        #       production. Suggest saving/using sk-learn MBKMeans class? Can
        #       the class be regenerated from an existing codebook?
        self._model_dir = osp.join(smqtk_config.DATA_DIR, model_directory)
        self._work_dir = osp.join(smqtk_config.WORK_DIR, work_directory)

        self._kmeans_k = int(kmeans_k)
        self._flann_target_precision = float(flann_target_precision)
        self._flann_sample_fraction = float(flann_sample_fraction)
        self._rand_seed = None if random_seed is None else int(random_seed)

        if self._rand_seed is not None:
            numpy.random.seed(self._rand_seed)

        # Cannot pre-load FLANN stuff because odd things happen when processing/
        # threading. Loading index file is fast anyway.
        self._codebook = None
        if self.has_model:
            self._codebook = numpy.load(self.codebook_filepath)

    @property
    def codebook_filepath(self):
        safe_create_dir(self._model_dir)
        return osp.join(self._model_dir,
                        "%s.codebook.npy" % (self.descriptor_type(),))

    @property
    def flann_index_filepath(self):
        safe_create_dir(self._model_dir)
        return osp.join(self._model_dir,
                        "%s.flann_index.dat" % (self.descriptor_type(),))

    @property
    def flann_params_filepath(self):
        safe_create_dir(self._model_dir)
        return osp.join(self._model_dir,
                        "%s.flann_params.json" % (self.descriptor_type(),))

    @property
    def has_model(self):
        has_model = (osp.isfile(self.codebook_filepath)
                     and osp.isfile(self.flann_index_filepath))
        # Load the codebook model if not already loaded. FLANN index will be
        # loaded when needed to prevent thread/subprocess memory issues.
        if self._codebook is None and has_model:
            self._codebook = numpy.load(self.codebook_filepath)
        return has_model

    @property
    def temp_dir(self):
        return safe_create_dir(osp.join(self._work_dir, 'temp_files'))

    @abc.abstractmethod
    def descriptor_type(self):
        """
        :return: String descriptor type as used by colorDescriptor
        :rtype: str
        """
        return

    @abc.abstractmethod
    def _generate_descriptor_matrices(self, data_set, **kwargs):
        """
        Generate info and descriptor matrices based on ingest type.

        :param data_set: Iterable of data elements to generate combined info
            and descriptor matrices for.
        :type item_iter: collections.Set[smqtk.data_rep.DataElement]

        :param limit: Limit the number of descriptor entries to this amount.
        :type limit: int

        :return: Combined info and descriptor matrices for all base images
        :rtype: (numpy.core.multiarray.ndarray, numpy.core.multiarray.ndarray)

        """
        pass

    def _get_checkpoint_dir(self, data):
        """
        The directory that contains checkpoint material for a given data element

        :param data: Data element
        :type data: smqtk.data_rep.DataElement

        :return: directory path
        :rtype: str

        """
        d = osp.join(self._work_dir, *partition_string(data.md5(), 8))
        safe_create_dir(d)
        return d

    def _get_standard_info_descriptors_filepath(self, data, frame=None):
        """
        Get the standard path to a data element's computed descriptor output,
        which for colorDescriptor consists of two matrices: info and descriptors

        :param data: Data element
        :type data: smqtk.data_rep.DataElement

        :param frame: frame within the data file
        :type frame: int

        :return: Paths to info and descriptor checkpoint numpy files
        :rtype: (str, str)

        """
        d = self._get_checkpoint_dir(data)
        if frame is not None:
            return (
                osp.join(d, "%s.info.%d.npy" % (data.md5(), frame)),
                osp.join(d, "%s.descriptors.%d.npy" % (data.md5(), frame))
            )
        else:
            return (
                osp.join(d, "%s.info.npy" % data.md5()),
                osp.join(d, "%s.descriptors.npy" % data.md5())
            )

    def _get_checkpoint_feature_file(self, data):
        """
        Return the standard path to a data element's computed feature checkpoint
        file relative to our current working directory.

        :param data: Data element
        :type data: smqtk.data_rep.DataElement

        :return: Standard path to where the feature checkpoint file for this
            given data element.
        :rtype: str

        """
        return osp.join(self._get_checkpoint_dir(data),
                        "%s.feature.npy" % data.md5())

    def generate_model(self, data_set, **kwargs):
        """
        Generate this feature detector's data-model given a file ingest. This
        saves the generated model to the currently configured data directory.

        For colorDescriptor, we generate raw features over the ingest data,
        compute a codebook via kmeans, and then create an index with FLANN via
        the "autotune" algorithm to intelligently pick the fastest indexing
        method.

        :param num_elements: Number of data elements in the iterator
        :type num_elements: int

        :param data_set: Set of input data elements to generate the model
            with.
        :type data_set: collections.Set[smqtk.data_rep.DataElement]

        """
        super(ColorDescriptor_Base, self).generate_model(data_set, **kwargs)

        if self.has_model:
            self.log.warn("ColorDescriptor model for descriptor type '%s' "
                          "already generated!", self.descriptor_type())
            return

        pyflann.set_distance_type(self.FLANN_DISTANCE_FUNCTION)
        flann = pyflann.FLANN()

        if not osp.isfile(self.codebook_filepath):
            self.log.info("Did not find existing ColorDescriptor codebook for "
                          "descriptor '%s'.", self.descriptor_type())

            # generate descriptors
            with SimpleTimer("Generating descriptor matrices...",
                             self.log.info):
                descriptors_checkpoint = osp.join(self._work_dir,
                                                  "model_descriptors.npy")

                if osp.isfile(descriptors_checkpoint):
                    self.log.debug("Found existing computed descriptors work "
                                   "file for model generation.")
                    descriptors = numpy.load(descriptors_checkpoint)
                else:
                    self.log.debug("Computing model descriptors")
                    _, descriptors = \
                        self._generate_descriptor_matrices(
                            data_set,
                            limit=self.CODEBOOK_DESCRIPTOR_LIMIT
                        )
                    _, tmp = tempfile.mkstemp(dir=self._work_dir,
                                              suffix='.npy')
                    self.log.debug("Saving model-gen info/descriptor matrix")
                    numpy.save(tmp, descriptors)
                    os.rename(tmp, descriptors_checkpoint)

            # Compute centroids (codebook) with kmeans
            with SimpleTimer("Computing sklearn.cluster.MiniBatchKMeans...",
                             self.log.info):
                kmeans_verbose = self.log.getEffectiveLevel <= logging.DEBUG
                kmeans = sklearn.cluster.MiniBatchKMeans(
                    n_clusters=self._kmeans_k,
                    init_size=self._kmeans_k*3,
                    random_state=self._rand_seed,
                    verbose=kmeans_verbose,
                    compute_labels=False,
                )
                kmeans.fit(descriptors)
                codebook = kmeans.cluster_centers_
            with SimpleTimer("Saving generated codebook...", self.log.debug):
                numpy.save(self.codebook_filepath, codebook)
        else:
            self.log.info("Found existing codebook file.")
            codebook = numpy.load(self.codebook_filepath)

        # create FLANN index
        # - autotune will force select linear search if there are < 1000 words
        #   in the codebook vocabulary.
        if self.log.getEffectiveLevel() <= logging.DEBUG:
            log_level = 'info'
        else:
            log_level = 'warning'
        with SimpleTimer("Building FLANN index...", self.log.info):
            p = {
                "target_precision": self._flann_target_precision,
                "sample_fraction": self._flann_sample_fraction,
                "log_level": log_level,
                "algorithm": "autotuned"
            }
            if self._rand_seed is not None:
                p['random_seed'] = self._rand_seed
            flann_params = flann.build_index(codebook, **p)
        with SimpleTimer("Saving FLANN index to file...", self.log.debug):
            # Save FLANN index data binary
            flann.save_index(self.flann_index_filepath)
            # Save out log of parameters
            with open(self.flann_params_filepath, 'w') as ofile:
                json.dump(flann_params, ofile, indent=4, sort_keys=True)

        # save generation results to class for immediate feature computation use
        self._codebook = codebook

    def compute_descriptor(self, data):
        """
        Given some kind of data, process and return a feature vector as a Numpy
        array.

        :raises RuntimeError: Feature extraction failure of some kind.

        :param data: Some kind of input data for the feature descriptor. This is
            descriptor dependent.
        :type data: smqtk.data_rep.DataElement

        :return: Feature vector. This is a histogram of N bins where N is the
            number of centroids in the codebook. Bin values is percent
            composition, not absolute counts.
        :rtype: numpy.ndarray

        """
        checkpoint_filepath = self._get_checkpoint_feature_file(data)
        if osp.isfile(checkpoint_filepath):
            return numpy.load(checkpoint_filepath)

        if not self.has_model:
            raise RuntimeError("No model currently loaded! Check the existence "
                               "or, or generate, model files!\n"
                               "Codebook path: %s\n"
                               "FLANN Index path: %s"
                               % (self.codebook_filepath,
                                  self.flann_index_filepath))

        self.log.debug("Computing descriptors for data UID[%s]...", data.uuid())
        info, descriptors = self._generate_descriptor_matrices({data})

        # Quantization
        # - loaded the model at class initialization if we had one
        self.log.debug("Quantizing descriptors")
        pyflann.set_distance_type(self.FLANN_DISTANCE_FUNCTION)
        flann = pyflann.FLANN()
        flann.load_index(self.flann_index_filepath, self._codebook)
        try:
            idxs, dists = flann.nn_index(descriptors)
        except AssertionError:

            self.log.error("Codebook shape  : %s", self._codebook.shape)
            self.log.error("Descriptor shape: %s", descriptors.shape)

            raise

        # Create histogram
        # - Using explicit bin slots to prevent numpy from automatically
        #   creating tightly constrained bins. This would otherwise cause
        #   histograms between two inputs to be non-comparable (unaligned bins).
        # - See numpy note about ``bins`` to understand why the +1 is necessary
        #: :type: numpy.core.multiarray.ndarray
        h = numpy.histogram(idxs,  # indices are all integers
                            bins=numpy.arange(self._codebook.shape[0] + 1))[0]
        # self.log.debug("Quantization histogram: %s", h)
        # Normalize histogram into relative frequencies
        # - Not using /= on purpose. h is originally int32 coming out of
        #   histogram. /= would keep int32 type when we want it to be
        #   transformed into a float type by the division.
        if h.sum():
            # noinspection PyAugmentAssignment
            h = h / float(h.sum())
        else:
            h = numpy.zeros(h.shape, h.dtype)
        # self.log.debug("Normalized histogram: %s", h)

        self.log.debug("Saving checkpoint feature file")
        if not osp.isdir(osp.dirname(checkpoint_filepath)):
            safe_create_dir(osp.dirname(checkpoint_filepath))
        numpy.save(checkpoint_filepath, h)

        return h


# noinspection PyAbstractClass,PyPep8Naming
class ColorDescriptor_Image (ColorDescriptor_Base):

    def valid_content_types(self):
        """
        :return: A set valid MIME type content types that this descriptor can
            handle.
        :rtype: set[str]
        """
        return {'image/bmp', 'image/jpeg', 'image/png', 'image/tiff'}

    def _generate_descriptor_matrices(self, data_set, **kwargs):
        """
        Generate info and descriptor matrices based on ingest type.

        :param data_set: Iterable of data elements to generate combined info
            and descriptor matrices for.
        :type item_iter: collections.Set[smqtk.data_rep.DataElement]

        :param limit: Limit the number of descriptor entries to this amount.
        :type limit: int

        :return: Combined info and descriptor matrices for all base images
        :rtype: (numpy.core.multiarray.ndarray, numpy.core.multiarray.ndarray)

        """
        if not data_set:
            raise ValueError("No data given to process.")

        inf = float('inf')
        descriptor_limit = kwargs.get('limit', inf)
        per_item_limit = numpy.floor(float(descriptor_limit) / len(data_set))

        if len(data_set) == 1:
            # because an iterable doesn't necessarily have a next() method
            di = iter(data_set).next()
            # Check for checkpoint files
            info_fp, desc_fp = \
                self._get_standard_info_descriptors_filepath(di)
            # Save out data bytes to temporary file
            temp_img_filepath = di.write_temp(self.temp_dir)
            try:
                # Generate descriptors
                utils.generate_descriptors(
                    self.PROC_COLORDESCRIPTOR, temp_img_filepath,
                    self.descriptor_type(), info_fp, desc_fp, per_item_limit
                )
            finally:
                # clean temp file
                di.clean_temp()
            return numpy.load(info_fp), numpy.load(desc_fp)
        else:
            # compute and V-stack matrices for all given images
            pool = multiprocessing.Pool(processes=self.PARALLEL)

            # Mapping of UID to tuple containing:
            #   (info_fp, desc_fp, async processing result, tmp_clean_method)
            r_map = {}
            with SimpleTimer("Computing descriptors async...", self.log.debug):
                for di in data_set:
                    # Creating temporary image file from data bytes
                    tmp_img_fp = di.write_temp(self.temp_dir)

                    info_fp, desc_fp = \
                        self._get_standard_info_descriptors_filepath(di)
                    args = (self.PROC_COLORDESCRIPTOR, tmp_img_fp,
                            self.descriptor_type(), info_fp, desc_fp)
                    r = pool.apply_async(utils.generate_descriptors, args)
                    r_map[di.uuid()] = (info_fp, desc_fp, r, di.clean_temp)
            pool.close()

            # Pass through results from descriptor generation, aggregating
            # matrix shapes.
            # - Transforms r_map into:
            #       UID -> (info_fp, desc_fp, starting_row, SubSampleIndices)
            self.log.debug("Constructing information for super matrices...")
            s_keys = sorted(r_map.keys())
            running_height = 0  # info and desc heights congruent
            # Known constants
            i_width = 5
            d_width = 384

            for uid in s_keys:
                ifp, dfp, r, tmp_clean_method = r_map[uid]

                # descriptor generation may have failed for this ingest UID
                try:
                    i_shape, d_shape = r.get()
                except RuntimeError:
                    self.log.warning("Descriptor generation failed for "
                                     "UID[%d], skipping its inclusion in "
                                     "model.", uid)
                    r_map[uid] = None
                    continue
                finally:
                    # Done with image file, so remove from filesystem
                    tmp_clean_method()

                if None in (i_width, d_width):
                    i_width = i_shape[1]
                    d_width = d_shape[1]

                ssi = None
                if i_shape[0] > per_item_limit:
                    # pick random indices to subsample down to size limit
                    ssi = sorted(
                        numpy.random.permutation(i_shape[0])[:per_item_limit]
                    )

                r_map[uid] = (ifp, dfp, running_height, ssi)
                running_height += min(i_shape[0], per_item_limit)
            pool.join()

            # Asynchronously load files, inserting data into master matrices
            self.log.debug("Building super matrices...")
            master_info = numpy.zeros((running_height, i_width), dtype=float)
            master_desc = numpy.zeros((running_height, d_width), dtype=float)
            tp = multiprocessing.pool.ThreadPool(processes=self.PARALLEL)
            for uid in s_keys:
                if r_map[uid]:
                    ifp, dfp, sR, ssi = r_map[uid]
                    tp.apply_async(ColorDescriptor_Image._thread_load_matrix,
                                   args=(ifp, master_info, sR, ssi))
                    tp.apply_async(ColorDescriptor_Image._thread_load_matrix,
                                   args=(dfp, master_desc, sR, ssi))
            tp.close()
            tp.join()
            return master_info, master_desc

    @staticmethod
    def _thread_load_matrix(filepath, m, sR, subsample=None):
        """
        load a numpy matrix from ``filepath``, inserting the loaded matrix into
        ``m`` starting at the row ``sR``.

        If subsample has a value, it will be a list if indices to
        """
        n = numpy.load(filepath)
        if subsample:
            n = n[subsample, :]
        m[sR:sR+n.shape[0], :n.shape[1]] = n


# noinspection PyAbstractClass,PyPep8Naming
class ColorDescriptor_Video (ColorDescriptor_Base):

    # # Custom higher limit for video since, ya know, they have multiple frames.
    CODEBOOK_DESCRIPTOR_LIMIT = 1500000

    FRAME_EXTRACTION_PARAMS = {
        "second_offset": 0.0,       # Start at beginning
        "second_interval": 0.5,     # Sample every 0.5 seconds
        "max_duration": 1.0,        # Cover full duration
        "output_image_ext": 'png',  # Output PNG files
        "ffmpeg_exe": "ffmpeg",
    }

    def valid_content_types(self):
        """
        :return: A set valid MIME type content types that this descriptor can
            handle.
        :rtype: set[str]
        """
        # At the moment, assuming ffmpeg can decode all video types, which it
        # probably cannot, but we'll filter this down when it becomes relevant.
        # noinspection PyUnresolvedReferences
        # TODO: GIF support?
        return set([x for x in mimetypes.types_map.values()
                    if x.startswith('video')])

    def _generate_descriptor_matrices(self, data_set, **kwargs):
        """
        Generate info and descriptor matrices based on ingest type.

        :param data_set: Iterable of data elements to generate combined info
            and descriptor matrices for.
        :type item_iter: collections.Set[smqtk.data_rep.DataElement]

        :param limit: Limit the number of descriptor entries to this amount.
        :type limit: int

        :return: Combined info and descriptor matrices for all base images
        :rtype: (numpy.core.multiarray.ndarray, numpy.core.multiarray.ndarray)

        """
        descriptor_limit = kwargs.get('limit', float('inf'))
        # With videos, an "item" is one video, so, collect for a while video
        # as normal, then subsample from the full video collection.
        per_item_limit = numpy.floor(float(descriptor_limit) / len(data_set))

        # If an odd number of jobs, favor descriptor extraction
        if self.PARALLEL:
            descr_parallel = max(1, math.ceil(self.PARALLEL/2.0))
            extract_parallel = max(1, math.floor(self.PARALLEL/2.0))
        else:
            cpuc = multiprocessing.cpu_count()
            descr_parallel = max(1, math.ceil(cpuc/2.0))
            extract_parallel = max(1, math.floor(cpuc/2.0))

        # For each video, extract frames and submit colorDescriptor processing
        # jobs for each frame, combining all results into a single matrix for
        # return.
        pool = multiprocessing.Pool(processes=descr_parallel)

        # Mapping of [UID] to [frame] to tuple containing:
        #   (info_fp, desc_fp, async processing result)
        r_map = {}
        with SimpleTimer("Extracting frames and submitting descriptor jobs...",
                         self.log.debug):
            for di in data_set:
                r_map[di.uuid()] = {}
                tmp_vid_fp = di.write_temp(self.temp_dir)
                p = dict(self.FRAME_EXTRACTION_PARAMS)
                vmd = get_metadata_info(tmp_vid_fp)
                p['second_offset'] = vmd.duration * p['second_offset']
                p['max_duration'] = vmd.duration * p['max_duration']
                fm = video_utils.ffmpeg_extract_frame_map(
                    tmp_vid_fp,
                    parallel=extract_parallel,
                    **p
                )

                # Compute descriptors for extracted frames.
                for frame, imgPath in fm.iteritems():
                    info_fp, desc_fp = \
                        self._get_standard_info_descriptors_filepath(di, frame)
                    r = pool.apply_async(
                        utils.generate_descriptors,
                        args=(self.PROC_COLORDESCRIPTOR, imgPath,
                              self.descriptor_type(), info_fp, desc_fp)
                    )
                    r_map[di.uuid()][frame] = (info_fp, desc_fp, r)

                # Clean temporary file while computing descriptors
                di.clean_temp()
        pool.close()

        # Each result is a tuple of two ndarrays: info and descriptor matrices
        with SimpleTimer("Collecting shape information for super matrices...",
                         self.log.debug):
            running_height = 0
            # Known constants
            i_width = 5
            d_width = 384

            # Transform r_map[uid] into:
            #   (info_mat_files, desc_mat_files, sR, ssi_list)
            #   -> files in frame order
            uids = sorted(r_map)
            for uid in uids:
                video_num_desc = 0
                video_info_mat_fps = []  # ordered list of frame info mat files
                video_desc_mat_fps = []  # ordered list of frame desc mat files
                for frame in sorted(r_map[uid]):
                    ifp, dfp, r = r_map[uid][frame]
                    i_shape, d_shape = r.get()
                    if None in (i_width, d_width):
                        i_width = i_shape[1]
                        d_width = d_shape[1]

                    video_info_mat_fps.append(ifp)
                    video_desc_mat_fps.append(dfp)
                    video_num_desc += i_shape[0]

                # If combined descriptor height exceeds the per-item limit,
                # generate a random subsample index list
                ssi = None
                if video_num_desc > per_item_limit:
                    ssi = sorted(
                        numpy.random.permutation(video_num_desc)[:per_item_limit]
                    )
                    video_num_desc = len(ssi)

                r_map[uid] = (video_info_mat_fps, video_desc_mat_fps,
                              running_height, ssi)
                running_height += video_num_desc
        pool.join()
        del pool

        with SimpleTimer("Building master descriptor matrices...",
                         self.log.debug):
            master_info = numpy.zeros((running_height, i_width), dtype=float)
            master_desc = numpy.zeros((running_height, d_width), dtype=float)
            tp = multiprocessing.pool.ThreadPool(processes=self.PARALLEL)
            for uid in uids:
                info_fp_list, desc_fp_list, sR, ssi = r_map[uid]
                tp.apply_async(ColorDescriptor_Video._thread_load_matrices,
                               args=(master_info, info_fp_list, sR, ssi))
                tp.apply_async(ColorDescriptor_Video._thread_load_matrices,
                               args=(master_desc, desc_fp_list, sR, ssi))
            tp.close()
            tp.join()

        return master_info, master_desc

    @staticmethod
    def _thread_load_matrices(m, file_list, sR, subsample=None):
        """
        load numpy matrices from files in ``file_list``, concatenating them
        vertically. If a list of row indices is provided in ``subsample`` we
        subsample those rows out of the concatenated matrix. This matrix is then
        inserted into ``m`` starting at row ``sR``.
        """
        c = numpy.load(file_list[0])
        for i in range(1, len(file_list)):
            c = numpy.vstack((c, numpy.load(file_list[i])))
        if subsample:
            c = c[subsample, :]
        m[sR:sR+c.shape[0], :c.shape[1]] = c


# Begin automatic class type creation
valid_descriptor_types = [
    'rgbhistogram',
    'opponenthistogram',
    'huehistogram',
    'nrghistogram',
    'transformedcolorhistogram',
    'colormoments',
    'colormomentinvariants',
    'sift',
    'huesift',
    'hsvsift',
    'opponentsift',
    'rgsift',
    'csift',
    'rgbsift',
]


def _create_image_descriptor_class(descriptor_type_str):
    """
    Create and return a ColorDescriptor class that operates over Image files
    using the given descriptor type.
    """
    assert descriptor_type_str in valid_descriptor_types, \
        "Given ColorDescriptor type was not valid! Given: %s. Expected one " \
        "of: %s" % (descriptor_type_str, valid_descriptor_types)

    # noinspection PyPep8Naming
    class _cd_image_impl (ColorDescriptor_Image):
        def descriptor_type(self):
            """
            :rtype: str
            """
            return descriptor_type_str

    _cd_image_impl.__name__ = "ColorDescriptor_Image_%s" % descriptor_type_str
    return _cd_image_impl


def _create_video_descriptor_class(descriptor_type_str):
    """
    Create and return a ColorDescriptor class that operates over Video files
    using the given descriptor type.
    """
    assert descriptor_type_str in valid_descriptor_types, \
        "Given ColorDescriptor type was not valid! Given: %s. Expected one " \
        "of: %s" % (descriptor_type_str, valid_descriptor_types)

    # noinspection PyPep8Naming
    class _cd_video_impl (ColorDescriptor_Video):
        def descriptor_type(self):
            """
            :rtype: str
            """
            return descriptor_type_str

    _cd_video_impl.__name__ = "ColorDescriptor_Video_%s" % descriptor_type_str
    return _cd_video_impl


# In order to allow multiprocessing, class types must be concretely assigned to
# variables in the module. Dynamic generation causes issues with pickling (the
# default data transmission protocol).

ColorDescriptor_Image_rgbhistogram = _create_image_descriptor_class('rgbhistogram')
ColorDescriptor_Image_opponenthistogram = _create_image_descriptor_class('opponenthistogram')
ColorDescriptor_Image_huehistogram = _create_image_descriptor_class('huehistogram')
ColorDescriptor_Image_nrghistogram = _create_image_descriptor_class('nrghistogram')
ColorDescriptor_Image_transformedcolorhistogram = _create_image_descriptor_class('transformedcolorhistogram')
ColorDescriptor_Image_colormoments = _create_image_descriptor_class('colormoments')
ColorDescriptor_Image_colormomentinvariants = _create_image_descriptor_class('colormomentinvariants')
ColorDescriptor_Image_sift = _create_image_descriptor_class('sift')
ColorDescriptor_Image_huesift = _create_image_descriptor_class('huesift')
ColorDescriptor_Image_hsvsift = _create_image_descriptor_class('hsvsift')
ColorDescriptor_Image_opponentsift = _create_image_descriptor_class('opponentsift')
ColorDescriptor_Image_rgsift = _create_image_descriptor_class('rgsift')
ColorDescriptor_Image_csift = _create_image_descriptor_class('csift')
ColorDescriptor_Image_rgbsift = _create_image_descriptor_class('rgbsift')

ColorDescriptor_Video_rgbhistogram = _create_video_descriptor_class('rgbhistogram')
ColorDescriptor_Video_opponenthistogram = _create_video_descriptor_class('opponenthistogram')
ColorDescriptor_Video_huehistogram = _create_video_descriptor_class('huehistogram')
ColorDescriptor_Video_nrghistogram = _create_video_descriptor_class('nrghistogram')
ColorDescriptor_Video_transformedcolorhistogram = _create_video_descriptor_class('transformedcolorhistogram')
ColorDescriptor_Video_colormoments = _create_video_descriptor_class('colormoments')
ColorDescriptor_Video_colormomentinvariants = _create_video_descriptor_class('colormomentinvariants')
ColorDescriptor_Video_sift = _create_video_descriptor_class('sift')
ColorDescriptor_Video_huesift = _create_video_descriptor_class('huesift')
ColorDescriptor_Video_hsvsift = _create_video_descriptor_class('hsvsift')
ColorDescriptor_Video_opponentsift = _create_video_descriptor_class('opponentsift')
ColorDescriptor_Video_rgsift = _create_video_descriptor_class('rgsift')
ColorDescriptor_Video_csift = _create_video_descriptor_class('csift')
ColorDescriptor_Video_rgbsift = _create_video_descriptor_class('rgbsift')


cd_type_list = [
    ColorDescriptor_Image_rgbhistogram,
    ColorDescriptor_Video_rgbhistogram,
    ColorDescriptor_Image_opponenthistogram,
    ColorDescriptor_Video_opponenthistogram,
    ColorDescriptor_Image_huehistogram,
    ColorDescriptor_Video_huehistogram,
    ColorDescriptor_Image_nrghistogram,
    ColorDescriptor_Video_nrghistogram,
    ColorDescriptor_Image_transformedcolorhistogram,
    ColorDescriptor_Video_transformedcolorhistogram,
    ColorDescriptor_Image_colormoments,
    ColorDescriptor_Video_colormoments,
    ColorDescriptor_Image_colormomentinvariants,
    ColorDescriptor_Video_colormomentinvariants,
    ColorDescriptor_Image_sift,
    ColorDescriptor_Video_sift,
    ColorDescriptor_Image_huesift,
    ColorDescriptor_Video_huesift,
    ColorDescriptor_Image_hsvsift,
    ColorDescriptor_Video_hsvsift,
    ColorDescriptor_Image_opponentsift,
    ColorDescriptor_Video_opponentsift,
    ColorDescriptor_Image_rgsift,
    ColorDescriptor_Video_rgsift,
    ColorDescriptor_Image_csift,
    ColorDescriptor_Video_csift,
    ColorDescriptor_Image_rgbsift,
    ColorDescriptor_Video_rgbsift,
]
