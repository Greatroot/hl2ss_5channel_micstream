
import os
import fractions
import tarfile
import csv
import numpy as np
import time
import cv2
import av
import hl2ss
import hl2ss_io
import hl2ss_3dcv


#------------------------------------------------------------------------------
# RM IMU
#------------------------------------------------------------------------------

def rm_imu_get_batch_size(port):
    if (port == hl2ss.StreamPort.RM_IMU_ACCELEROMETER):
        return hl2ss.Parameters_RM_IMU_ACCELEROMETER.BATCH_SIZE
    if (port == hl2ss.StreamPort.RM_IMU_GYROSCOPE):
        return hl2ss.Parameters_RM_IMU_GYROSCOPE.BATCH_SIZE
    if (port == hl2ss.StreamPort.RM_IMU_MAGNETOMETER):
        return hl2ss.Parameters_RM_IMU_MAGNETOMETER.BATCH_SIZE


#------------------------------------------------------------------------------
# Microphone
#------------------------------------------------------------------------------

def microphone_planar_to_packed(array):
    data = np.zeros((1, array.size), dtype=array.dtype)
    data[0, 0::2] = array[0, :]
    data[0, 1::2] = array[1, :]
    return data


def microphone_packed_to_planar(array):
    data = np.zeros((2, array.size // 2), dtype=array.dtype)
    data[0, :] = array[0, 0::2]
    data[1, :] = array[0, 1::2]
    return data


class microphone_resampler:
    def create(self, target_format=None, target_layout=None, target_rate=None):
        self._resampler = av.AudioResampler(format=target_format, layout=target_layout, rate=target_rate)

    def resample(self, data, profile):
        in_frame = av.AudioFrame.from_ndarray(data, format='s16' if (profile == hl2ss.AudioProfile.RAW) else 'fltp', layout='stereo')
        in_frame.rate = hl2ss.Parameters_MICROPHONE.SAMPLE_RATE
        out_frames = self._resampler.resample(in_frame)
        return [frame.to_ndarray() for frame in out_frames]


#------------------------------------------------------------------------------
# SI
#------------------------------------------------------------------------------

class _SI_Hand:
    def __init__(self, poses, orientations, positions, radii, accuracies):
        self.poses = poses
        self.orientations = orientations
        self.positions = positions
        self.radii = radii
        self.accuracies = accuracies


def si_unpack_hand(hand):
    poses = [hand.get_joint_pose(joint) for joint in range(0, hl2ss.SI_HandJointKind.TOTAL)]
    orientations = np.array([pose.orientation for pose in poses])
    positions = np.array([pose.position for pose in poses])
    radii = np.array([pose.radius for pose in poses])
    accuracies = np.array([pose.accuracy for pose in poses])
    return _SI_Hand(poses, orientations, positions, radii, accuracies)


def si_head_pose_rotation_matrix(up, forward):
    y = up
    z = -forward
    x = np.cross(y, z)
    return np.hstack((x, y, z)).reshape((3, 3)).transpose()


def si_ray_to_vector(origin, direction):
    return np.vstack((origin, direction)).reshape((-1, 6))


def si_ray_get_origin(ray):
    return ray[:, 0:3]


def si_ray_get_direction(ray):
    return ray[:, 3:6]


def si_ray_transform(ray, transform4x4):
    return np.hstack((hl2ss_3dcv.transform(ray[:, 0:3], transform4x4), hl2ss_3dcv.orient(ray[:, 3:6], transform4x4)))


def si_ray_to_point(ray, d):
    return (ray[:, 0:3] + d * ray[:, 3:6]).reshape((-1, 3))


class _SI_JointName:
    OF = [
        'Palm',
        'Wrist',
        'ThumbMetacarpal',
        'ThumbProximal',
        'ThumbDistal',
        'ThumbTip',
        'IndexMetacarpal',
        'IndexProximal',
        'IndexIntermediate',
        'IndexDistal',
        'IndexTip',
        'MiddleMetacarpal',
        'MiddleProximal',
        'MiddleIntermediate',
        'MiddleDistal',
        'MiddleTip',
        'RingMetacarpal',
        'RingProximal',
        'RingIntermediate',
        'RingDistal',
        'RingTip',
        'LittleMetacarpal',
        'LittleProximal',
        'LittleIntermediate',
        'LittleDistal',
        'LittleTip',
    ]


def si_get_joint_name(joint_kind):
    return _SI_JointName.OF[joint_kind]


#------------------------------------------------------------------------------
# Draw
#------------------------------------------------------------------------------

def draw_points(image, points, radius, color, thickness):
    for x, y in points:
        if (x >= 0 and y >= 0 and x < image.shape[1] and y < image.shape[0]):
            cv2.circle(image, (x, y), radius, color, thickness)
    return image


#------------------------------------------------------------------------------
# Unpacking
#------------------------------------------------------------------------------

def _create_csv_header_for_timestamp():
    return ['timestamp']


def _create_csv_header_for_pose():
    return [f'pose {i}{j}' for i in range(0, 4) for j in range(0, 4)]


def _create_csv_header_for_rm_imu_sample(index):
    return [f'sensor ticks {index}', f'soc ticks {index}', f'x {index}', f'y {index}', f'z {index}', f'temperature {index}']


def _create_csv_header_for_rm_imu_payload(port):
    header = []
    for i in range(0, rm_imu_get_batch_size(port)):
        header.extend(_create_csv_header_for_rm_imu_sample(i))
    return header


def _create_csv_header_for_pv_payload():
    return ['fx', 'fy', 'cx', 'cy']


def _create_csv_header_for_si_head_pose():
    return ['head pose valid'] + [f'head position {w}' for w in ['x', 'y', 'z']] + [f'head forward {w}' for w in ['x', 'y', 'z']] + [f'head up {w}' for w in ['x', 'y', 'z']]


def _create_csv_header_for_si_eye_ray():
    return ['eye ray valid'] + [f'eye ray origin {w}'for w in ['x', 'y', 'z']] + [f'eye ray direction {w}' for w in ['x', 'y', 'z']]


def _create_csv_header_for_si_hand_joint(hand_name, joint_kind):
    joint_name = si_get_joint_name(joint_kind)
    return [f'{hand_name} {joint_name} position {u}' for u in ['x', 'y', 'z']] + [f'{hand_name} {joint_name} orientation {u}' for u in ['x', 'y', 'z', 'w']] + [f'{hand_name} {joint_name} radius'] + [f'{hand_name} {joint_name} accuracy']


def _create_csv_header_for_si_hand(hand_name):
    header = [f'hand {hand_name} valid']
    for joint_index in range(0, hl2ss.SI_HandJointKind.TOTAL):
        header.extend(_create_csv_header_for_si_hand_joint(hand_name, joint_index))
    return header


def _create_csv_header_for_si_payload():
    return _create_csv_header_for_si_head_pose() + _create_csv_header_for_si_eye_ray() + _create_csv_header_for_si_hand('left') + _create_csv_header_for_si_hand('right')


def _create_csv_header_for_eet_calibration():
    return ['calibration valid']


def _create_csv_header_for_eet_ray(ray_name):
    return [f'{ray_name} valid'] + [f'{ray_name} origin {w}'for w in ['x', 'y', 'z']] + [f'{ray_name} direction {w}' for w in ['x', 'y', 'z']]


def _create_csv_header_for_eet_field(field_name):
    return [f'{field_name} valid', f'{field_name} value']


def _create_csv_header_for_eet_payload():
    return  _create_csv_header_for_eet_calibration() + _create_csv_header_for_eet_ray('combined') + _create_csv_header_for_eet_ray('left') + _create_csv_header_for_eet_ray('right') + _create_csv_header_for_eet_field('left openness') + _create_csv_header_for_eet_field('right openness') + _create_csv_header_for_eet_field('vergence distance')


def _create_csv_header_for_rm_vlc():
    return _create_csv_header_for_timestamp() + _create_csv_header_for_pose()


def _create_csv_header_for_rm_depth():
    return _create_csv_header_for_timestamp() + _create_csv_header_for_pose()


def _create_csv_header_for_rm_imu(port):
    return _create_csv_header_for_timestamp() + _create_csv_header_for_rm_imu_payload(port) + _create_csv_header_for_pose()


def _create_csv_header_for_pv():
    return _create_csv_header_for_timestamp() + _create_csv_header_for_pv_payload() + _create_csv_header_for_pose()


def _create_csv_header_for_microphone():
    return _create_csv_header_for_timestamp()


def _create_csv_header_for_si():
    return _create_csv_header_for_timestamp() + _create_csv_header_for_si_payload()


def _create_csv_header_for_eet():
    return _create_csv_header_for_timestamp() + _create_csv_header_for_eet_payload() + _create_csv_header_for_pose()


def _create_csv_row_for_timestamp(timestamp):
    return [str(timestamp)]


def _create_csv_row_for_pose(pose):
    if (pose is None):
        pose = np.zeros((4, 4), dtype=np.float32)
    return pose.astype(str).flatten().tolist()


def _create_csv_row_for_rm_imu_frame(frame):
    return [str(frame.vinyl_hup_ticks), str(frame.soc_ticks), str(frame.x), str(frame.y), str(frame.z), str(frame.temperature)]


def _create_csv_row_for_rm_imu_payload(payload):
    frames = []
    for i in range(0, payload.get_count()):
        frames.extend(_create_csv_row_for_rm_imu_frame(payload.get_frame(i)))
    return frames


def _create_csv_row_for_pv_payload(payload):
    return payload.focal_length.astype(str).tolist() + payload.principal_point.astype(str).tolist()


def _create_csv_row_for_si_head_pose(valid, pose):
    return [str(valid)] + pose.position.astype(str).tolist() + pose.forward.astype(str).tolist() + pose.up.astype(str).tolist()


def _create_csv_row_for_si_eye_ray(valid, ray):
    return [str(valid)] + ray.origin.astype(str).tolist() + ray.direction.astype(str).tolist()


def _create_csv_row_for_si_hand_joint(pose):
    return pose.position.astype(str).tolist() + pose.orientation.astype(str).tolist() + pose.radius.astype(str).tolist() + pose.accuracy.tostr(str).tolist()


def _create_csv_row_for_si_hand(valid, hand):
    row = [str(valid)]
    for joint_index in range(0, hl2ss.SI_HandJointKind.TOTAL):
        row.extend(_create_csv_row_for_si_hand_joint(hand.get_joint_pose(joint_index)))
    return row


def _create_csv_row_for_si_payload(payload):
    return _create_csv_row_for_si_head_pose(payload.is_valid_head_pose(), payload.get_head_pose()) + _create_csv_row_for_si_eye_ray(payload.is_valid_eye_ray(), payload.get_eye_ray()) + _create_csv_row_for_si_hand(payload.is_valid_hand_left(), payload.get_hand_left()) + _create_csv_row_for_si_hand(payload.is_valid_hand_right(), payload.get_hand_right())


def _create_csv_row_for_eet_calibration(valid):
    return [str(valid)]


def _create_csv_row_for_eet_ray(valid, ray):
    return [str(valid)] + ray.origin.astype(str).tolist() + ray.direction.astype(str).tolist()


def _create_csv_row_for_eet_field(valid, value):
    return [str(valid)] + value.astype(str).tolist()


def _create_csv_row_for_eet_payload(payload):
    return _create_csv_row_for_eet_calibration(payload.calibration_valid) + _create_csv_row_for_eet_ray(payload.combined_ray_valid, payload.combined_ray) + _create_csv_row_for_eet_ray(payload.left_ray_valid, payload.left_ray) + _create_csv_row_for_eet_ray(payload.right_ray_valid, payload.right_ray) + _create_csv_row_for_eet_field(payload.left_openness_valid, payload.left_openness) + _create_csv_row_for_eet_field(payload.right_openness_valid, payload.right_openness) + _create_csv_row_for_eet_field(payload.vergence_distance_valid, payload.vergence_distance)


def _create_csv_row_for_rm_vlc(data):
    return _create_csv_row_for_timestamp(data.timestamp) + _create_csv_row_for_pose(data.pose)


def _create_csv_row_for_rm_depth(data):
    return _create_csv_row_for_timestamp(data.timestamp) + _create_csv_row_for_pose(data.pose)


def _create_csv_row_for_rm_imu(data):
    return _create_csv_row_for_timestamp(data.timestamp) + _create_csv_row_for_rm_imu_payload(data.payload) + _create_csv_row_for_pose(data.pose)


def _create_csv_row_for_pv(data):
    return _create_csv_row_for_timestamp(data.timestamp) + _create_csv_row_for_pv_payload(data.payload) + _create_csv_row_for_pose(data.pose)


def _create_csv_row_for_microphone(data):
    return _create_csv_row_for_timestamp(data.timestamp)


def _create_csv_row_for_si(data):
    return _create_csv_row_for_timestamp(data.timestamp) + _create_csv_row_for_si_payload(data.payload)


def _create_csv_row_for_eet(data):
    return _create_csv_row_for_timestamp(data.timestamp) + _create_csv_row_for_eet_payload(data.payload) + _create_csv_row_for_pose(data.pose)


def _create_csv_header(port):
    if (port == hl2ss.StreamPort.RM_VLC_LEFTFRONT):
        return _create_csv_header_for_rm_vlc()
    if (port == hl2ss.StreamPort.RM_VLC_LEFTLEFT):
        return _create_csv_header_for_rm_vlc()
    if (port == hl2ss.StreamPort.RM_VLC_RIGHTFRONT):
        return _create_csv_header_for_rm_vlc()
    if (port == hl2ss.StreamPort.RM_VLC_RIGHTRIGHT):
        return _create_csv_header_for_rm_vlc()
    if (port == hl2ss.StreamPort.RM_DEPTH_AHAT):
        return _create_csv_header_for_rm_depth()
    if (port == hl2ss.StreamPort.RM_DEPTH_LONGTHROW):
        return _create_csv_header_for_rm_depth()
    if (port == hl2ss.StreamPort.RM_IMU_ACCELEROMETER):
        return _create_csv_header_for_rm_imu(port)
    if (port == hl2ss.StreamPort.RM_IMU_GYROSCOPE):
        return _create_csv_header_for_rm_imu(port)
    if (port == hl2ss.StreamPort.RM_IMU_MAGNETOMETER):
        return _create_csv_header_for_rm_imu(port)
    if (port == hl2ss.StreamPort.PERSONAL_VIDEO):
        return _create_csv_header_for_pv()
    if (port == hl2ss.StreamPort.MICROPHONE):
        return _create_csv_header_for_microphone()
    if (port == hl2ss.StreamPort.SPATIAL_INPUT):
        return _create_csv_header_for_si()
    if (port == hl2ss.StreamPort.EXTENDED_EYE_TRACKER):
        return _create_csv_header_for_eet()


def _create_csv_row(port, data):
    if (port == hl2ss.StreamPort.RM_VLC_LEFTFRONT):
        return _create_csv_row_for_rm_vlc(data)
    if (port == hl2ss.StreamPort.RM_VLC_LEFTLEFT):
        return _create_csv_row_for_rm_vlc(data)
    if (port == hl2ss.StreamPort.RM_VLC_RIGHTFRONT):
        return _create_csv_row_for_rm_vlc(data)
    if (port == hl2ss.StreamPort.RM_VLC_RIGHTRIGHT):
        return _create_csv_row_for_rm_vlc(data)
    if (port == hl2ss.StreamPort.RM_DEPTH_AHAT):
        return _create_csv_row_for_rm_depth(data)
    if (port == hl2ss.StreamPort.RM_DEPTH_LONGTHROW):
        return _create_csv_row_for_rm_depth(data)
    if (port == hl2ss.StreamPort.RM_IMU_ACCELEROMETER):
        data.payload = hl2ss.unpack_rm_imu(data.payload)
        return _create_csv_row_for_rm_imu(data)
    if (port == hl2ss.StreamPort.RM_IMU_GYROSCOPE):
        data.payload = hl2ss.unpack_rm_imu(data.payload)
        return _create_csv_row_for_rm_imu(data)
    if (port == hl2ss.StreamPort.RM_IMU_MAGNETOMETER):
        data.payload = hl2ss.unpack_rm_imu(data.payload)
        return _create_csv_row_for_rm_imu(data)
    if (port == hl2ss.StreamPort.PERSONAL_VIDEO):
        data.payload = hl2ss.unpack_pv(data.payload)
        return _create_csv_row_for_pv(data)
    if (port == hl2ss.StreamPort.MICROPHONE):
        return _create_csv_row_for_microphone(data)
    if (port == hl2ss.StreamPort.SPATIAL_INPUT):
        data.payload = hl2ss.unpack_si(data.payload)
        return _create_csv_row_for_si(data)
    if (port == hl2ss.StreamPort.EXTENDED_EYE_TRACKER):
        data.payload = hl2ss.unpack_eet(data.payload)
        return _create_csv_row_for_eet(data)


def unpack_to_csv(input_filename, output_filename):    
    rd = hl2ss_io.create_rd(False, input_filename, hl2ss.ChunkSize.SINGLE_TRANSFER, None)
    rd.open()

    port = rd.header.port

    wr = open(output_filename, 'w')
    csv_wr = csv.writer(wr)
    csv_wr.writerow(_create_csv_header(port))

    while (True):
        data = rd.read()
        if (data is None):
            break
        csv_wr.writerow(_create_csv_row(port, data))

    wr.close()
    rd.close()


def get_av_codec_name(port, profile):
    if (port == hl2ss.StreamPort.RM_VLC_LEFTFRONT):
        return hl2ss.get_video_codec_name(profile)
    if (port == hl2ss.StreamPort.RM_VLC_LEFTLEFT):
        return hl2ss.get_video_codec_name(profile)
    if (port == hl2ss.StreamPort.RM_VLC_RIGHTFRONT):
        return hl2ss.get_video_codec_name(profile)
    if (port == hl2ss.StreamPort.RM_VLC_RIGHTRIGHT):
        return hl2ss.get_video_codec_name(profile)
    if (port == hl2ss.StreamPort.RM_DEPTH_AHAT):
        return hl2ss.get_video_codec_name(profile)
    if (port == hl2ss.StreamPort.PERSONAL_VIDEO):
        return hl2ss.get_video_codec_name(profile)
    if (port == hl2ss.StreamPort.MICROPHONE):
        return hl2ss.get_audio_codec_name(profile)


def get_av_framerate(port):
    if (port == hl2ss.StreamPort.RM_VLC_LEFTFRONT):
        return hl2ss.Parameters_RM_VLC.FPS
    if (port == hl2ss.StreamPort.RM_VLC_LEFTLEFT):
        return hl2ss.Parameters_RM_VLC.FPS
    if (port == hl2ss.StreamPort.RM_VLC_RIGHTFRONT):
        return hl2ss.Parameters_RM_VLC.FPS
    if (port == hl2ss.StreamPort.RM_VLC_RIGHTRIGHT):
        return hl2ss.Parameters_RM_VLC.FPS
    if (port == hl2ss.StreamPort.RM_DEPTH_AHAT):
        return hl2ss.Parameters_RM_DEPTH_AHAT.FPS
    if (port == hl2ss.StreamPort.RM_DEPTH_LONGTHROW):
        return hl2ss.Parameters_RM_DEPTH_LONGTHROW.FPS
    if (port == hl2ss.StreamPort.MICROPHONE):
        return hl2ss.Parameters_MICROPHONE.SAMPLE_RATE


def unpack_to_mp4(input_filenames, output_filename):
    readers = [hl2ss_io.create_rd(False, input_filename, hl2ss.ChunkSize.SINGLE_TRANSFER, None) for input_filename in input_filenames]
    [reader.open() for reader in readers]

    container = av.open(output_filename, mode='w')
    streams = [container.add_stream(get_av_codec_name(reader.header.port, reader.header.profile), rate=get_av_framerate(reader.header.port) if (get_av_framerate(reader.header.port) is not None) else reader.header.framerate) for reader in readers]
    codecs = [av.CodecContext.create(get_av_codec_name(reader.header.port, reader.header.profile), "r") for reader in readers]

    base = hl2ss._RANGEOF.U64_MAX

    for reader in readers:
        data = reader.read()
        if ((data is not None) and (data.timestamp < base)):
            base = data.timestamp

    [reader.close() for reader in readers]
    [reader.open() for reader in readers]

    time_base = fractions.Fraction(1, hl2ss.TimeBase.HUNDREDS_OF_NANOSECONDS)

    for reader, codec, stream in zip(readers, codecs, streams):
        while (True):
            data = reader.read()
            if (data is None):
                break

            if (reader.header.port == hl2ss.StreamPort.PERSONAL_VIDEO):
                payload = hl2ss.unpack_pv(data.payload).image
            else:
                payload = data.payload

            local_timestamp = data.timestamp - base

            for packet in codec.parse(payload):
                packet.stream = stream
                packet.pts = local_timestamp
                packet.dts = local_timestamp
                packet.time_base = time_base
                container.mux(packet)

    container.close()
    [reader.close() for reader in readers]


def unpack_to_png(input_filename, output_filename):
    rd = hl2ss_io.create_rd(True, input_filename, hl2ss.ChunkSize.SINGLE_TRANSFER, None)
    rd.open()

    tar = tarfile.open(output_filename, 'w')
    idx = 0

    while (True):
        data = rd.read()
        if (data is None):
            break
        tar.addfile(tarfile.TarInfo(f'depth_{idx}.png'), cv2.imencode('.png', data.payload.depth)[1])
        tar.addfile(tarfile.TarInfo(f'ab_{idx}.png'), cv2.imencode('.png', data.payload.ab)[1])
        idx += 1

    tar.close()
    rd.close()


#------------------------------------------------------------------------------
# Timing
#------------------------------------------------------------------------------

class continuity_analyzer:
    def __init__(self, period):
        self._period = period
        self._ub = 1.5 * self._period
        self._lb = 0.5 * self._period
        self._last = None

    def push(self, timestamp):
        if (self._last is None):
            status = (0, -1)
        else:
            delta = timestamp - self._last
            status = (1, delta) if (delta > self._ub) else (-1, delta) if (delta < self._lb) else (0, delta)
        self._last = timestamp
        return status


class framerate_counter:
    def reset(self):
        self._count = 0
        self._start = time.perf_counter()

    def increment(self):
        self._count += 1
        return self._count

    def delta(self):
        return time.perf_counter() - self._start

    def get(self):
        return self._count / self.delta()


class stream_report:
    def __init__(self, notify_period, stream_period):
        self._np = notify_period
        self._ca = continuity_analyzer(stream_period)
        self._fc = framerate_counter()
        self._fc.reset()

    def _report_continuity(self, timestamp):
        status, delta = self._ca.push(timestamp)
        if (status != 0):
            print('Discontinuity detected with delta time {delta}'.format(delta=delta))

    def _report_framerate_and_pose(self, timestamp, pose):
        self._fc.increment()
        if (self._fc.delta() >= self._np):
            print('FPS: {fps}'.format(fps=self._fc.get()))
            print('Pose at {timestamp}'.format(timestamp=timestamp))
            print(pose)
            self._fc.reset()

    def push(self, data):
        self._report_continuity(data.timestamp)
        self._report_framerate_and_pose(data.timestamp, data.pose)

