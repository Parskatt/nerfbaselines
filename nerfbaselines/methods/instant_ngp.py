# pylint: disable=import-error,import-outside-toplevel
import math
import contextlib
import json
import os
import numpy as np
from pathlib import Path
from typing import Optional, Iterable
import tempfile
from PIL import Image, ImageOps
from ..distortion import Distortions, CameraModel
from ..types import Dataset, Method, MethodInfo, ProgressCallback, CurrentProgress
from ..backends import DockerMethod
from ..registry import MethodSpec


AABB_SCALE = 32


def sharpness(imagePath):
    import cv2
    image = cv2.imread(imagePath)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def rotmat(a, b):
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v = np.cross(a, b)
    c = np.dot(a, b)
    # handle exception for the opposite direction input
    if c < -1 + 1e-10:
        return rotmat(a + np.random.uniform(-1e-2, 1e-2, 3), b)
    s = np.linalg.norm(v)
    kmat = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + kmat + kmat.dot(kmat) * ((1 - c) / (s ** 2 + 1e-10))

def closest_point_2_lines(oa, da, ob, db): # returns point closest to both rays of form o+t*d, and a weight factor that goes to 0 if the lines are parallel
    da = da / np.linalg.norm(da)
    db = db / np.linalg.norm(db)
    c = np.cross(da, db)
    denom = np.linalg.norm(c)**2
    t = ob - oa
    ta = np.linalg.det([t, db, c]) / (denom + 1e-10)
    tb = np.linalg.det([t, da, c]) / (denom + 1e-10)
    if ta > 0:
        ta = 0
    if tb > 0:
        tb = 0
    return (oa+ta*da+ob+tb*db) * 0.5, denom


def get_transforms(dataset: Dataset, dataparser_transform=None, dataparser_scale=None, aabb_scale=None, keep_coords=None, **kwargs):
    frames = []
    for i in range(len(dataset)):
        camera = {}
        camera["w"] = int(dataset.image_sizes[i, 0])
        camera["h"] = int(dataset.image_sizes[i, 1])
        camera["fl_x"] = float(dataset.camera_intrinsics[i, 0])
        camera["fl_y"] = float(dataset.camera_intrinsics[i, 1])
        camera["cx"] = dataset.camera_intrinsics[i, 2]
        camera["cy"] = dataset.camera_intrinsics[i, 3]
        camera["k1"] = 0
        camera["k2"] = 0
        camera["k3"] = 0
        camera["k4"] = 0
        camera["p1"] = 0
        camera["p2"] = 0
        camera["is_fisheye"] = False
        if dataset.camera_distortions is not None:
            cam_type = dataset.camera_distortions.camera_types[i]
            if cam_type == CameraModel.PINHOLE.value:
                pass
            elif cam_type == CameraModel.OPENCV.value or cam_type == CameraModel.OPENCV_FISHEYE.value:
                camera["k1"] = dataset.camera_distortions.distortion_params[i, 0]
                camera["k2"] = dataset.camera_distortions.distortion_params[i, 1]
                camera["k3"] = dataset.camera_distortions.distortion_params[i, 2]
                camera["k4"] = dataset.camera_distortions.distortion_params[i, 3]
                camera["p1"] = dataset.camera_distortions.distortion_params[i, 4]
                camera["p2"] = dataset.camera_distortions.distortion_params[i, 5]
                if cam_type == CameraModel.OPENCV_FISHEYE.value:
                    camera["is_fisheye"] = True
            else:
                raise NotImplementedError(f"Camera model {cam_type} not supported")
        # fl = 0.5 * w / tan(0.5 * angle_x);
        camera["camera_angle_x"] = math.atan(camera["w"] / (camera["fl_x"] * 2)) * 2
        camera["camera_angle_y"] = math.atan(camera["h"] / (camera["fl_y"] * 2)) * 2
        # camera["fovx"] = camera["camera_angle_x"] * 180 / math.pi
        # camera["fovy"] = camera["camera_angle_y"] * 180 / math.pi
        frame = camera.copy()
        bottom = np.array([0.0, 0.0, 0.0, 1.0]).reshape([1, 4])
        up = np.zeros(3)
        name = str(dataset.file_paths[i])
        b = sharpness(name) if os.path.exists(name) else 1.0
        c2w = dataset.camera_poses[i, :3, :4]
        c2w = np.concatenate([c2w[0:3, 0:4], bottom], 0)

        if not keep_coords:
            c2w = c2w[[1,0,2,3],:]
            c2w[2,:] *= -1 # flip whole world upside down

        up += c2w[0:3,1]

        frame["file_path"] = name
        # Adding sharpness triggers removal in ingp code if the file doesn't exist
        # frame["sharpness"] = b
        frame["transform_matrix"] = c2w
        frames.append(frame)

    nframes = len(frames)
    if dataparser_transform is None and not keep_coords:
        # don't keep colmap coords - reorient the scene to be easier to work with
        up = up / np.linalg.norm(up)
        R = rotmat(up,[0,0,1]) # rotate up vector to [0,0,1]
        R = np.pad(R,[0,1])
        R[-1, -1] = 1

        # find a central point they are all looking at
        totw = 0.0
        totp = np.array([0.0, 0.0, 0.0])
        for f in frames:
            mf = f["transform_matrix"][0:3,:]
            for g in frames:
                mg = g["transform_matrix"][0:3,:]
                p, w = closest_point_2_lines(mf[:,3], mf[:,2], mg[:,3], mg[:,2])
                if w > 0.00001:
                    totp += p*w
                    totw += w
        if totw > 0.0:
            totp /= totw
        offset_mat = np.eye(4, dtype=R.dtype)
        offset_mat[0:3,3] = -totp

        for f in frames:
            f["transform_matrix"][0:3,3] -= totp
        dataparser_transform = R
    elif dataparser_transform is None:
        dataparser_transform = np.eye(4, dtype=np.float32)

    # Compute scale
    if dataparser_scale is None and not keep_coords:
        if not keep_coords:
            avglen = 0.
            for f in frames:
                avglen += np.linalg.norm(f["transform_matrix"][0:3,3])
            avglen /= nframes
            dataparser_scale = float(4.0 / avglen) # scale to "nerf sized"
    elif dataparser_scale is None:
        dataparser_scale = 1.0

    for f in frames:
        f["transform_matrix"] = np.matmul(dataparser_transform, f["transform_matrix"]) # rotate up to be the z axis
        f["transform_matrix"][0:3,3] *= dataparser_scale
        f["transform_matrix"] = f["transform_matrix"].tolist()

    # TODO: handle masks
    assert dataset.sampling_mask_paths is None
    # output_mask = np.zeros((img.shape[0], img.shape[1]))
    # NOTE: mask is 1 for rays to skip and 0 for rays to use
    # rgb_path = Path(frame["file_path"])
    # mask_name = str(rgb_path.parents[0] / Path("dynamic_mask_" + rgb_path.name.replace(".jpg", ".png")))
    # cv2.imwrite(mask_name, (output_mask*255).astype(np.uint8))
    out = {
        "frames": frames
    }
    if aabb_scale is not None:
        out["aabb_scale"] = aabb_scale
    return out, dict(
        dataparser_transform=dataparser_transform, 
        dataparser_scale=dataparser_scale,
        aabb_scale=aabb_scale,
        keep_coords=keep_coords,
        **kwargs)


class InstantNGP(Method):
    def __init__(self, checkpoint: Optional[Path] = None, **kwargs):
        super().__init__(**kwargs)
        self.checkpoint = Path(checkpoint) if checkpoint is not None else None
        self._train_transforms = None
        self.testbed = None
        self.n_steps = None
        self.dataparser_params = None
        self._tempdir = tempfile.TemporaryDirectory()
        self._tempdir.__enter__()

    @property
    def info(self):
        return MethodInfo(
            supports_undistortion=True)

    def _setup(self, train_transforms):
        import pyngp as ngp
        testbed = ngp.Testbed()
        testbed.root_dir = os.path.dirname(train_transforms)
        testbed.load_training_data(str(train_transforms))
        if self.checkpoint is not None:
            testbed.load_snapshot(str(self.checkpoint / "checkpoint.ingp"))
        else:
            package_root = Path(os.path.dirname(os.path.dirname(os.path.abspath(ngp.__file__))))
            testbed.reload_network_from_file(str(package_root/"configs"/"nerf"/"base.json"))

        # Default parameters from scripts/run.py
        testbed.nerf.sharpen = 0.0
        testbed.exposure = 0.0
        testbed.shall_train = True
        testbed.nerf.render_with_lens_distortion = True

        if self.dataparser_params.get("nerf_compatibility", False):
            print("NeRF compatibility mode enabled")

            # Prior nerf papers accumulate/blend in the sRGB
            # color space. This messes not only with background
            # alpha, but also with DOF effects and the likes.
            # We support this behavior, but we only enable it
            # for the case of synthetic nerf data where we need
            # to compare PSNR numbers to results of prior work.
            testbed.color_space = ngp.ColorSpace.SRGB

            # No exponential cone tracing. Slightly increases
            # quality at the cost of speed. This is done by
            # default on scenes with AABB 1 (like the synthetic
            # ones), but not on larger scenes. So force the
            # setting here.
            testbed.nerf.cone_angle_constant = 0

            # Match nerf paper behaviour and train on a fixed bg.
            testbed.nerf.training.random_bg_color = False
        self.testbed = testbed

    def setup_train(self, train_dataset: Dataset, *, num_iterations: int):
        # Write images
        from tqdm import tqdm
        tmpdir = self._tempdir.name
        for i, impath_source in enumerate(tqdm(train_dataset.file_paths, desc="caching images")):
            impath_source = Path(impath_source)
            impath_target = Path(tmpdir) / impath_source.relative_to(train_dataset.file_paths_root)
            impath_target.parent.mkdir(parents=True, exist_ok=True)
            if impath_target.exists():
                continue
            if impath_source.exists():
                impath_target.symlink_to(impath_source)
            else:
                image = Image.fromarray(train_dataset.images[i])
                image.save(str(impath_target.with_suffix(".png")))
            if train_dataset.sampling_masks is not None:
                mask = train_dataset.sampling_masks[i]
                mask = Image.fromarray(mask, mode="L")
                mask = ImageOps.invert(mask)
                mask.save(str(impath_target.with_name(f"dynamic_mask_{impath_target.name}")))

        current_step = 0
        if self.checkpoint is not None:
            with (self.checkpoint / "train_transforms.json").open() as f:
                self._train_transforms = json.load(f)
            with (self.checkpoint / "meta.json").open() as f:
                meta = json.load(f)
                self.dataparser_params = meta["dataparser_params"]
                current_step = meta["step"]
                self.dataparser_params["dataparser_transform"] = np.array(self.dataparser_params["dataparser_transform"], dtype=np.float32)
        else:
            loader = train_dataset.metadata.get("type")
            nerf_compatibility = False
            if loader == "blender":
                aabb_scale = None
                keep_coords = True
                nerf_compatibility = True
            else:
                aabb_scale = AABB_SCALE
                keep_coords = False
                nerf_compatibility = False
            self._train_transforms, self.dataparser_params = get_transforms(train_dataset,
                                                                            aabb_scale=aabb_scale,
                                                                            keep_coords=keep_coords,
                                                                            nerf_compatibility=nerf_compatibility)
        with (Path(tmpdir) / "transforms.json").open("w") as f:
            json.dump(self._train_transforms, f)
        assert "nerf_compatibility" in self.dataparser_params
        self._setup(Path(tmpdir) / "transforms.json")
        assert self.testbed.training_step == current_step, "Training step mismatch"

    def _setup_eval(self):
        assert self.checkpoint is not None
        current_step = 0
        with tempfile.TemporaryDirectory() as tmpdir:
            with (self.checkpoint / "train_transforms.json").open() as f:
                self._train_transforms = json.load(f)
            with (self.checkpoint / "meta.json").open() as f:
                meta = json.load(f)
                self.dataparser_params = meta["dataparser_params"]
                current_step = meta["step"]
                self.dataparser_params["dataparser_transform"] = np.array(self.dataparser_params["dataparser_transform"], dtype=np.float32)
            with (Path(tmpdir) / "transforms.json").open("w") as f:
                json.dump(self._train_transforms, f)
            self._setup(Path(tmpdir) / "transforms.json")
        self.testbed.training_step = current_step

    def train_iteration(self, step: int):
        current_frame = self.testbed.training_step
        while current_frame < step:
            if not self.testbed.frame():
                raise RuntimeError("Training failed")
            current_frame = self.testbed.training_step
        return {
            "loss": self.testbed.loss,
        }

    def save(self, path: Path):
        os.makedirs(path, exist_ok=True)
        with (path/"meta.json").open("w") as f:
            out = self.dataparser_params.copy()
            out["dataparser_transform"] = out["dataparser_transform"].tolist()
            json.dump({
                "dataparser_params": out,
                "step": self.testbed.training_step,
            }, f, offset=2)
        with (path/"train_transforms.json").open("w") as f:
            json.dump(self._train_transforms, f, offset=2)
        self.testbed.save_snapshot(path / "checkpoint.ingp", False)

    @contextlib.contextmanager
    def _with_eval_setup(self, 
                         poses: np.ndarray,
                         intrinsics: np.ndarray,
                         sizes: np.ndarray,
                         nears_fars: np.ndarray,
                         distortions: Optional[Distortions] = None):
        tmpdir = self._tempdir.name
        dataset = Dataset(
            camera_poses=poses,
            camera_intrinsics_normalized=intrinsics/sizes[..., :1],
            image_sizes=sizes,
            nears_fars=nears_fars,
            camera_distortions=distortions,
            sampling_mask_paths=None,
            file_paths=[f"{i:06d}.png" for i in range(len(poses))])
        with (Path(tmpdir) / "transforms.json").open("w") as f:
            json.dump(get_transforms(dataset, **self.dataparser_params)[0], f)

        old_testbed_background_color = self.testbed.background_color
        old_testbed_snap_to_pixel_centers = self.testbed.snap_to_pixel_centers
        old_testbed_render_min_transmittance = self.testbed.nerf.render_min_transmittance
        old_testbed_shall_train = self.testbed.shall_train
        try:
            # Evaluate metrics on black background
            self.testbed.background_color = [0.0, 0.0, 0.0, 1.0]

            # Prior nerf papers don't typically do multi-sample anti aliasing.
            # So snap all pixels to the pixel centers.
            self.testbed.snap_to_pixel_centers = True

            self.testbed.nerf.render_min_transmittance = 1e-4

            self.testbed.shall_train = False
            self.testbed.load_training_data(str(Path(tmpdir) / "transforms.json"))
            yield self.testbed

        finally:
            self.testbed.background_color = old_testbed_background_color
            self.testbed.snap_to_pixel_centers = old_testbed_snap_to_pixel_centers
            self.testbed.nerf.render_min_transmittance = old_testbed_render_min_transmittance
            self.testbed.shall_train = old_testbed_shall_train
            with (Path(tmpdir) / "transforms.json").open("w") as f:
                json.dump(self._train_transforms, f)
            self.testbed.load_training_data(str(Path(tmpdir) / "transforms.json"))

    def render(self,
               poses: np.ndarray,
               intrinsics: np.ndarray,
               sizes: np.ndarray,
               nears_fars: np.ndarray,
               distortions: Optional[Distortions] = None,
               progress_callback: Optional[ProgressCallback] = None) -> Iterable[np.ndarray]:
        if self.dataparser_params is None:
            self._setup_eval()
        with self._with_eval_setup(poses, intrinsics, sizes, nears_fars, distortions) as testbed:
            spp = 8
            if progress_callback:
                progress_callback(CurrentProgress(0, len(poses), 0, len(poses)))
            for i in range(testbed.training.dataset.n_images):
                resolution = testbed.nerf.training.dataset.metadata[i].resolution
                image = testbed.render(resolution[0], resolution[1], spp, True)
                yield image.detach().cpu().numpy()
                if progress_callback:
                    progress_callback(CurrentProgress(i+1, len(poses), i+1, len(poses)))

    def close(self):
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None


InstantNGPSpec = MethodSpec(
    method=InstantNGP,
    docker=DockerMethod.wrap(
        InstantNGP,
        image="kulhanek/ingp:latest",
        python_path="python3",
        home_path="/root"))
InstantNGPSpec.register("instant-ngp")
