# Offline public-image benchmark; install isolated dependencies per PERFORMANCE.md.
import pathlib,time,json,statistics,os
os.environ['DO_NOT_TRACK']='1'
import cv2, numpy as np, mediapipe as mp, openvino as ov
root=pathlib.Path(__file__).resolve().parents[1]; assets=pathlib.Path(os.environ.get('HOLOHAND_BENCH_ASSETS', str(pathlib.Path.home()/'.local/state/holohand/backend-benchmark')))
cv2.setNumThreads(1)
image=cv2.imread(str(assets/'thumb_up.jpg'));image=cv2.resize(image,(640,480))
frames=[]
for n in range(100):
 m=np.float32([[1,0,25*np.sin(n*.1)],[0,1,15*np.cos(n*.1)]])
 frames.append(cv2.warpAffine(image,m,(640,480)))
def stats(x):return {'mean_ms':statistics.mean(x),'p95_ms':float(np.percentile(x,95))}
report={}
options=mp.tasks.vision.HandLandmarkerOptions(base_options=mp.tasks.BaseOptions(model_asset_path=str(assets/'hand_landmarker.task')),running_mode=mp.tasks.vision.RunningMode.VIDEO,num_hands=1,min_hand_detection_confidence=.7,min_hand_presence_confidence=.8,min_tracking_confidence=.5)
with mp.tasks.vision.HandLandmarker.create_from_options(options) as model:
 times=[];valid=0
 for n,f in enumerate(frames):
  t=time.perf_counter();result=model.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,data=cv2.cvtColor(f,cv2.COLOR_BGR2RGB)),n*34)
  if n>=10: times.append((time.perf_counter()-t)*1000);valid+=bool(result.hand_landmarks)
 report['mediapipe_video']={**stats(times),'valid_frames':valid,'frames':len(times)}
print(json.dumps(report),flush=True)
core=ov.Core();report['devices']=core.available_devices
for dev in core.available_devices:
 report[dev]={}
 for name in ['palm_detection_mediapipe_2023feb.onnx','handpose_estimation_mediapipe_2023feb.onnx']:
  try:
   opts={'PERFORMANCE_HINT':'LATENCY'}
   if dev=='CPU':opts.update({'INFERENCE_NUM_THREADS':2,'NUM_STREAMS':1,'INFERENCE_PRECISION_HINT':ov.Type.f32})
   model=core.compile_model(str(root/'models'/name),dev,opts)
   inp=np.ones(model.input().shape,dtype=np.float32)*.5;times=[]
   for i in range(60):
    t=time.perf_counter(); model([inp]);
    if i>=10:times.append((time.perf_counter()-t)*1000)
   report[dev][name]=stats(times)
  except Exception as e:report[dev][name]={'error':str(e)}
 print(dev,json.dumps(report[dev]),flush=True)
(assets/'backend-probe.json').write_text(json.dumps(report,indent=2))
