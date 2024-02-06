import os
import shutil
import time
from datetime import datetime
from threading import Lock, RLock
from typing import Generator
from uuid import uuid4

import numpy as np
from PIL import Image
from redis.commands.search.document import Document
from torch import Tensor
from tqdm import tqdm

from db.vector.redis_client import BatchedPipeline
from knowledge_base.image.image_embedder import ImageEmbedder
from library.image.image_lib_table import ImageLibTable
from library.image.image_lib_vector_db import ImageLibVectorDb
from library.lib_base import *
from utils.exceptions.task_errors import (LockAcquisitionFailure,
                                          TaskCancellationException)
from utils.lock_context import LockContext
from utils.tqdm_context import TqdmContext


class ImageLib(LibraryBase):
    """Define an image library
    - Each image library will have only one table for storing images' metadata, such as UUID, path, filename, etc.
    """

    def __init__(self,
                 lib_path: str,
                 lib_name: str,
                 uuid: str,
                 local_mode: bool = True):
        """
        Args:
            lib_path (str): Path to the library
            uuid (str): UUID of the library
            local_mode (bool, optional): True for use local index, False for use Redis. Defaults to True.
        """
        if not uuid or not lib_name:
            raise LibraryError('Invalid UUID or library name')
        super().__init__(lib_path)

        # Load metadata
        with TqdmContext('Loading library metadata...', 'Loaded'):
            if not self.metadata_exists():
                initial_metadata: dict = BASIC_metadata | {
                    'type': 'image',
                    'uuid': uuid,
                    'name': lib_name,
                    'last_scanned': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }
                self.initialize_metadata(initial_metadata)
            else:
                self.load_metadata(uuid, lib_name)
        if not self._metadata or not self.uuid:
            raise LibraryError('Library metadata not initialized')

        self.local_mode: bool = local_mode
        self.__table: ImageLibTable | None = None
        self.__vector_db: ImageLibVectorDb | None = None
        self.__embedder: ImageEmbedder | None = None

        # File scan is mutually exclusive, use a lock to prevent concurrent scan
        self.__scan_lock = Lock()

    """
    Private methods
    """

    def __write_embedding_entry(self, relative_path: str, embedding: list[float], save_pipeline: BatchedPipeline | None = None):
        """Write an image entry (file info + embedding) to both DB and vector DB
        - An UUID is generated to identify the image globally
        """
        timestamp: datetime = datetime.now()
        uuid: str = str(uuid4())
        parent_folder: str = os.path.dirname(relative_path)
        filename: str = os.path.basename(relative_path)
        # (timestamp, uuid, path, filename)
        self.__table.insert_row((timestamp, uuid, parent_folder, filename))  # type: ignore
        self.__vector_db.add(uuid, embedding, save_pipeline)  # type: ignore

        # Record scan history on saving the embedding to the DB
        self.get_embedded_files()[relative_path] = uuid

    def __do_scan(self,
                  progress_reporter: Callable[[int], None] | None,
                  incremental: bool = False,
                  scan_only: bool = False) -> Generator[tuple[str, list[float] | None], None, None]:
        """Scan the library and embed the images on the fly

        Args:
            progress_reporter (Callable[[int], None] | None): The progress reporter function to report the progress to task manager
            incremental (bool, optional): If this is an incremental run. Defaults to False.
            scan_only (bool, optional): If do file scan only (without embedding). Defaults to False.

        Yields:
            Generator[tuple[str, list[float] | None], None, None]: _description_
        """
        all_files: set[str] = set()
        to_be_embedded: set[str] = set()
        for root, _, filenames in os.walk(self._path_lib):
            for filename in filenames:
                # Relative path built from os.path.relpath does not start with os.path.sep, no need to strip
                file_relative_path: str = os.path.relpath(os.path.join(root, filename), self._path_lib)
                all_files.add(file_relative_path)
                if incremental:
                    if file_relative_path in self.get_embedded_files():
                        continue
                    to_be_embedded.add(file_relative_path)

        if incremental:
            # For incremental run, check if there are files are deleted but left in embedded files list
            to_be_deleted: set[str] = set(self.get_embedded_files().keys()) - all_files
            all_files.clear()
            if to_be_deleted:
                self.remove_embeddings(list(to_be_deleted))
            tqdm.write(f'Incremental scan, found {len(to_be_embedded)} new files in {self._path_lib}')
        else:
            to_be_embedded = all_files
            tqdm.write(f'Library scanned, found {len(all_files)} files in {self._path_lib}')

        total: int = len(to_be_embedded)
        previous_progress: int = -1
        for i, relative_path in tqdm(enumerate(to_be_embedded), desc=f'Processing images', unit='item', ascii=' |'):
            try:
                # Validate if the file is an image and insert it into the table
                # - After verify() the file stream is closed, need to reopen it
                img: Image.Image = Image.open(os.path.join(self._path_lib, relative_path))
                img.verify()
                img = Image.open(os.path.join(self._path_lib, relative_path))
            except:
                tqdm.write(f'Invalid image: {relative_path}, skip')
                continue

            # If reporter is given, report progress to task manager
            # - Reduce report frequency, only report when progress changes
            current_progress: int = int(i / total * 100)
            if current_progress > previous_progress:
                previous_progress = current_progress
                self.report_progress(progress_reporter, current_progress)

            if not scan_only:
                start_time: float = time.time()
                embedding: list[float] = self.__embedder.embed_image_as_list(img)  # type: ignore
                time_taken: float = time.time() - start_time
                tqdm.write(f'Image embedded: {relative_path}, dimension: {len(embedding)}, cost: {time_taken:.2f}s')
                yield relative_path, embedding
            else:
                yield relative_path, None

    def __scan_and_initialize(self,
                              progress_reporter: Callable[[int], None] | None,
                              cancel_event: Event | None,
                              scan_only: bool = False):
        """Get all files and relative path info under self.path_lib, including files in all sub folders under lib_path
        """
        save_pipeline: BatchedPipeline | None = None
        try:
            save_pipeline = self.__vector_db.get_save_pipeline(batch_size=200)  # type: ignore
        except NotImplementedError:
            pass

        dimension: int = -1
        if save_pipeline:
            # Use batched pipeline when available
            with save_pipeline:
                for relative_path, embedding in self.__do_scan(progress_reporter, scan_only=scan_only):
                    if cancel_event is not None and cancel_event.is_set():
                        tqdm.write(f'Library initialization cancelled')
                        raise TaskCancellationException('Library initialization cancelled')

                    if not scan_only:
                        if not embedding:
                            raise LibraryError('Invalid embedding')
                        if dimension == -1:
                            dimension = len(embedding)
                        self.__write_embedding_entry(relative_path, embedding, save_pipeline)
        else:
            # Otherwise, save the embedding one by one
            for relative_path, embedding in self.__do_scan(progress_reporter):
                if cancel_event is not None and cancel_event.is_set():
                    tqdm.write(f'Library initialization cancelled')
                    raise TaskCancellationException('Library initialization cancelled')

                # If this is the first embedding and in local mode, initialize the index first as memory vector DB needs to build index before adding data
                if not scan_only:
                    if not embedding:
                        raise LibraryError('Invalid embedding')
                    if dimension == -1 and self.local_mode:
                        dimension = len(embedding)
                        with TqdmContext('Creating index...', 'Index created'):
                            self.__vector_db.initialize_index(dimension)  # type: ignore
                    self.__write_embedding_entry(relative_path, embedding)

        if not scan_only:
            self.__vector_db.persist()  # type: ignore
            self._save_scan_profile()
            if not self.local_mode:
                with TqdmContext('Creating index...', 'Index created'):
                    self.__vector_db.initialize_index(dimension)  # type: ignore
            tqdm.write(f'Image library DB initialized')
        else:
            tqdm.write(f'Image library scanned')

    """
    Overridden public methods from LibraryBase
    """

    def lib_is_ready(self) -> bool:
        if not self.metadata_exists():
            return False
        if not self.__table:
            return False
        if not self.__vector_db:
            return False
        if not self.__embedder:
            return False
        return True

    def initialize(self,
                   force_init: bool = False,
                   progress_reporter: Callable[[int], None] | None = None,
                   cancel_event: Event | None = None):
        ready: bool = self.lib_is_ready()
        if ready and not force_init:
            return

        # Load SQL DB and vector DB, and "not ready" has two cases:
        # 1. The lib is a new lib
        # 2. The lib is an existing lib but not loaded
        if not ready:
            if not self.__embedder:
                raise LibraryError('Embedder not set')
            self.__table = ImageLibTable(self._path_lib_data)
            self.__vector_db = ImageLibVectorDb(use_redis=not self.local_mode,
                                                lib_uuid=self._metadata['uuid'],
                                                data_folder=self._path_lib_data)

        # If DBs are all loaded (case#2, an existing lib) and not force init, return directly
        if not force_init and self.__table.row_count() > 0 and not self.__vector_db.db_is_empty():  # type: ignore
            return

        # Refresh ready status, initialize the library for force init or new lib cases
        ready = self.lib_is_ready()
        if ready:
            with TqdmContext(f'Forcibly re-initializing library: {self._path_lib}, purging existing library data...', 'Cleaned'):
                self.__vector_db.clean_all_data()  # type: ignore
                self.__table.clean_all_data()  # type: ignore
        else:
            tqdm.write(f'Initialize library DB: {self._path_lib} for new library')

        # Do full scan and initialize the lib
        with LockContext(self.__scan_lock) as lock:
            if not lock.acquired:
                raise LockAcquisitionFailure('There is already a scan task running')

            try:
                self._metadata['last_scanned'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self._save_metadata()
                self.__scan_and_initialize(progress_reporter, cancel_event)
            except TaskCancellationException:
                # On cancel, clean the DB
                self.__vector_db.clean_all_data()  # type: ignore
                self.__table.clean_all_data()  # type: ignore

    def demolish(self):
        """Delete the image library, it purges all library data
        1. Delete vector index, delete file directly if local mode, otherwise delete from Redis
        2. Delete DB file
        3. Delete metadata file

        Simply purge the library data folder
        """
        with LockContext(self.__scan_lock) as lock:
            if not lock.acquired:
                raise LockAcquisitionFailure('There is already a scan task running, cancel the task and try again')

            if not self.local_mode:
                if not self.__vector_db:
                    raise LibraryError('For Redis vector DB, the library must be initialized before demolish')
                self.__vector_db.delete_db()

            self.__embedder = None
            self.__table = None
            self.__vector_db = None
            shutil.rmtree(self._path_lib_data)

    def add_file(self, folder_relative_path: str, source_file: str):
        pass

    def delete_files(self, relative_paths: list[str], **kwargs):
        if not relative_paths:
            return

        for relative_path in relative_paths:
            if not relative_path:
                continue

            relative_path = relative_path.lstrip(os.path.sep)
            image_path: str = os.path.join(self._path_lib, relative_path)
            if os.path.isfile(image_path):
                os.remove(image_path)
        self.remove_embeddings(relative_paths)

    """
    Public methods
    """

    def set_embedder(self, embedder: ImageEmbedder):
        self.__embedder = embedder

    @ensure_lib_is_ready
    def remove_embeddings(self, relative_paths: list[str]):
        """Remove the embedding of an image from the DB
        """
        if not relative_paths:
            return

        for relative_path in relative_paths:
            if not relative_path:
                continue

            relative_path = relative_path.lstrip(os.path.sep)
            if relative_path in self.get_embedded_files():
                uuid: str = self.get_embedded_files()[relative_path]
                self.__vector_db.remove(uuid)  # type: ignore
                self.__table.delete_row_by_uuid(uuid)  # type: ignore
                self.get_embedded_files().pop(relative_path)
        self._save_scan_profile()

    def get_scan_gap(self) -> int:
        """Get the time gap from last scan in days
        """
        last_scanned: datetime = datetime.strptime(self._metadata['last_scanned'], '%Y-%m-%d %H:%M:%S')
        return (datetime.now() - last_scanned).days

    def incremental_initialization(self,
                                   progress_reporter: Callable[[int], None] | None = None,
                                   cancel_event: Event | None = None):
        """Incrementally initialize the library
        - Embedded images are skipped
        - Only embed the new images that are not embedded yet and add them to the DB
        - If an embedded image is deleted, also remove embedding entry from the DB
        """
        # If there is no single embedded image, do full scan directly
        if not self.get_embedded_files():
            self.initialize(True, progress_reporter, cancel_event)
            return

        ready: bool = self.lib_is_ready()

        # Load SQL DB and vector DB, and "not ready" has two cases:
        # 1. The lib is a new lib
        # 2. The lib is an existing lib but not loaded
        if not ready:
            if not self.__embedder:
                raise LibraryError('Embedder not set')
            self.__table = ImageLibTable(self._path_lib_data)
            self.__vector_db = ImageLibVectorDb(use_redis=not self.local_mode,
                                                lib_uuid=self._metadata['uuid'],
                                                data_folder=self._path_lib_data)

    """
    Query methods
    """

    @ensure_lib_is_ready
    def image_for_image_search(self, img: Image.Image, top_k: int = 10, extra_params: dict | None = None) -> list[tuple]:
        if not img or not top_k or top_k <= 0:
            return list()

        start: float = time.time()
        image_embedding: np.ndarray = self.__embedder.embed_image(img)  # type: ignore
        docs: list = self.__vector_db.query(np.asarray([image_embedding]), top_k)  # type: ignore
        time_taken: float = time.time() - start
        tqdm.write(f'Image search with image similarity completed, cost: {time_taken:.2f}s')

        # Parse the result, get file data from DB
        # - The local key of an image is `uuid` or `id` of the vector in the index
        # - The redis key of an image is `lib_uuid`:`img_uuid`
        res: list[tuple] = list()
        if self.local_mode:
            casted_local: list[int | str] = docs
            for possible_uuid in casted_local:
                if isinstance(possible_uuid, int):
                    raise LibraryError('ID tracking not enabled, cannot get UUID')
                row: tuple | None = self.__table.select_row_by_uuid(possible_uuid)  # type: ignore
                if row:
                    res.append(row)
        else:
            casted_redis: list[Document] = docs
            for doc in casted_redis:
                uuid: str = doc.id.split(':')[1]
                row: tuple | None = self.__table.select_row_by_uuid(uuid)  # type: ignore
                if row:
                    res.append(row)

        return res

    @ensure_lib_is_ready
    def text_for_image_search(self, text: str, top_k: int = 10, extra_params: dict | None = None) -> list[tuple]:
        if not text or not top_k or top_k <= 0:
            return list()

        start: float = time.time()
        # Text embedding is a 2D array, the first element is the embedding of the text
        text_embedding: np.ndarray = self.__embedder.embed_text(text)[0]  # type: ignore
        docs: list = self.__vector_db.query(np.asarray([text_embedding]), top_k)  # type: ignore
        time_taken: float = time.time() - start
        tqdm.write(f'Image search with text similarity completed, cost: {time_taken:.2f}s')

        # Parse the result, get file data from DB
        # - The local key of an image is `uuid` or `id` of the vector in the index
        # - The redis key of an image is `lib_uuid`:`img_uuid`
        res: list[tuple] = list()
        if self.local_mode:
            casted_local: list[int | str] = docs
            for possible_uuid in casted_local:
                if isinstance(possible_uuid, int):
                    raise LibraryError('ID tracking not enabled, cannot get UUID')
                row: tuple | None = self.__table.select_row_by_uuid(possible_uuid)  # type: ignore
                if row:
                    res.append(row)
        else:
            casted_redis: list[Document] = docs
            for doc in casted_redis:
                uuid: str = doc.id.split(':')[1]
                row: tuple | None = self.__table.select_row_by_uuid(uuid)  # type: ignore
                if row:
                    res.append(row)

        return res

    @ensure_lib_is_ready
    def text_image_similarity(self, tokens: list[str], img: Image.Image) -> dict[str, float]:
        if not tokens or not img:
            return dict()

        probs: Tensor = self.__embedder.compute_text_image_similarity(tokens, img)  # type: ignore
        res: dict[str, float] = dict()
        for i in range(len(tokens)):
            res[tokens[i]] = probs[0][i].item()

        return res
