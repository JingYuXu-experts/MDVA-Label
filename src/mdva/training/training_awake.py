"""Prevent idle sleep for one verified training PID; never restart any job."""
import argparse,time
from pathlib import Path
import psutil
from .runtime import now,exclusive
from .durable import atomic_json
from .watchdog import keep_awake
ROOT=Path(__file__).resolve().parents[3]
TRAIN_MODULES = (
    'mdva.training.scss_main', 'mdva.training.scss_resume', 'mdva.training.fl_learning_study',
    'mdva.training.fl_rate_study', 'mdva.training.fl_main', 'mdva.training.fl_sampling_control',
    'mdva.training.fl_contrast_study', 'mdva.training.ar_scale_study', 'mdva.training.ar_small_region_study',
    'mdva.training.ar_continuum_study', 'mdva.training.ar_patch_refiner',
)


def run(study,pid,birth):
    study=Path(study).resolve()
    if not study.is_relative_to((ROOT/'out').resolve()) or not (study/'protocol.json').is_file():raise ValueError('未登记训练目录')
    def owner():
        try:
            p=psutil.Process(pid);args=p.cmdline()
            if abs(p.create_time()-birth)>.01 or not any(m in args for m in TRAIN_MODULES):return False
            return (ROOT/args[args.index('--study')+1]).resolve()==study and p.is_running()
        except (psutil.NoSuchProcess,psutil.AccessDenied,ValueError,IndexError):return False
    if not owner():raise ValueError('训练PID或进程创建时间不符')
    pause = study / 'run/PAUSE'
    args = psutil.Process(pid).cmdline()
    paired_modules = {
        'mdva.training.fl_contrast_study': ('control', 'contrast'),
        'mdva.training.ar_scale_study': ('512', '1024'),
        'mdva.training.ar_continuum_study': ('control', 'continuum'),
    }
    paired = next((allowed for module, allowed in paired_modules.items() if module in args), None)
    if paired:
        arm = args[args.index('--arm') + 1]
        if arm not in paired:
            raise ValueError('未登记的配对组')
        pause = study / arm / 'run/PAUSE'
    with exclusive(study/'awake.lock'):
        awake=keep_awake(True)
        try:
            while owner() and not pause.exists():
                atomic_json(study/'awake-status.json',{'state':'watching_verified_training','pid':pid,'birth':birth,
                    'system_sleep_inhibited':awake,'updated_utc':now(),'automatic_restart':False})
                time.sleep(15)
        finally:
            keep_awake(False)
            atomic_json(study/'awake-status.json',{'state':'released','pid':pid,'system_sleep_inhibited':False,'updated_utc':now(),'automatic_restart':False})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',required=True);p.add_argument('--pid',type=int,required=True);p.add_argument('--birth',type=float,required=True);a=p.parse_args();run(a.study,a.pid,a.birth)
