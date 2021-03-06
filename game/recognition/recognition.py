from multiprocessing import Process
from contextlib import contextmanager
from abc import abstractmethod

import numpy as np
import cv2

from game.config import CONFIG
from game.events.tasks import *
from game.events.results import *
from game.database import FaceDatabase
from game.recognition.models import init_model_stack
from game.recognition.face import Face
from game.monitoring import monitor_runtime


class WorkerProcess(Process):
    def __init__(self, task_queue, result_queue, error_queue):
        super(WorkerProcess, self).__init__()
        self.task_queue = task_queue
        self.result_queue = result_queue
        self.error_queue = error_queue

    @abstractmethod
    def execute_task(self, task):
        pass

    def get_task(self):
        if not self.task_queue.empty():
            return True, self.task_queue.get()
        else:
            return False, None

    def run(self):
        try:
            while True:
                task_available, task = self.get_task()
                if not task_available:
                    continue

                if isinstance(task, Shutdown):
                    break

                result = self.execute_task(task)
                if result:
                    self.result_queue.put(result)

            # make sure to empty the queue
            while True:
                task_available, _ = self.get_task()
                if not task_available:
                    break

        except Exception as e:
            self.error_queue.put(Error())
            raise e  # to throw stacktrace and stop this process


class RecognitionProcess(WorkerProcess):
    def __init__(self, task_queue, result_queue, error_queue):
        super(RecognitionProcess, self).__init__(task_queue, result_queue, error_queue)

        self.models = None
        self.face_database = FaceDatabase(CONFIG["recognition"]["database"]["location"])

    def execute_task(self, task):
        if not self.models:
            self.models = init_model_stack()

        if isinstance(task, RecognizeFaces):
            bgr_image = task.image.data
            rgb_image = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)

            faces = self.__detect_faces(rgb_image, bgr_image)
            if len(faces) > 0:
                self._crop(rgb_image, faces)
                self._extract_features(faces)
                self._find_matches(faces)
            return RecognitionResult(faces)

        elif isinstance(task, BackupFaceDatabase):
            self.face_database.store()

        elif isinstance(task, RegisterFaces):
            if task.recognition_result is None or task.recognition_result.faces is None:
                return RegistrationResult(persons=[])

            registered = self.face_database.add_all(task.recognition_result.faces)
            return RegistrationResult(persons=registered)

        elif isinstance(task, UnregisterMostRecentFaces):
            unregistered = self.face_database.remove_most_recent()
            return UnregistrationResult(persons=unregistered)

        else:
            raise NotImplementedError()

    @monitor_runtime
    def __detect_faces(self, rgb_image, bgr_image):
        bounding_boxes = self.models.detect_faces(rgb_image)
        thumbnails = [bgr_image[box.top(): box.bottom(), box.left(): box.right(), :] for box in bounding_boxes]
        faces = [Face(bounding_box=box, thumbnail=thumbnail) for box, thumbnail in zip(bounding_boxes, thumbnails)]
        return faces

    @monitor_runtime
    def _crop(self, rgb_image, faces):
        for face in faces:
            face.landmarks = self.models.find_landmarks(rgb_image, face.bounding_box)
            face.crop = self.models.crop(rgb_image, face.landmarks)

    @monitor_runtime
    def _extract_features(self, faces):
        all_features = self.models.extract_features(np.array([face.crop for face in faces]))
        for face, features in zip(faces, all_features):
            face.features = features

    @monitor_runtime
    def _find_matches(self, faces):
        num_matches = CONFIG["display"]["num_matches_debug"]
        for face in faces:
            face.matches = self.models.match_faces(face.features, self.face_database.faces)[:num_matches]


@contextmanager
def init_recognition(work_queue, results_queue, error_queue):
    recognition = RecognitionProcess(work_queue, results_queue, error_queue)

    try:
        recognition.start()
        yield recognition
    finally:
        work_queue.put(Shutdown())
