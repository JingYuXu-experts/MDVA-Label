"""Replay the failed CH epoch diagnostically; never overwrite formal weights."""
from pathlib import Path
import json
import math


def main():
    import torch
    from mdva.training.train import load_checkpoint, seed_all, restore_rng, loader_for, loss_value
    from mdva.training.stable_models import architecture
    from mdva.training.datasets import SegmentationDataset
    from mdva.training.runtime import exclusive, atomic_json
    root = Path(__file__).resolve().parents[1]
    exp = root/'out/training-20261112-v2'
    saved = load_checkpoint(exp/'runs/CH-C2-s17/last.pt'); config = saved['identity']['config']
    with exclusive(root/'.mdva/training-gpu.lock'):
        seed_all(config['seed'])
        model, _ = architecture(config); model.load_state_dict(saved['model']); model.cuda().train()
        optimizer = torch.optim.Adam(model.parameters(), lr=.001); optimizer.load_state_dict(saved['optimizer'])
        for state in optimizer.state.values():
            for key, value in state.items():
                if torch.is_tensor(value) and key != 'step': state[key] = value.cuda()
        scaler = torch.amp.GradScaler('cuda'); scaler.load_state_dict(saved['scaler']); restore_rng(saved['rng'])
        dataset = SegmentationDataset(root/'out/pretraining-20261112/dataset-ready', 'CH', 'train', 512)
        optimizer.zero_grad(set_to_none=True)
        for step, batch in enumerate(loader_for(dataset, config, saved['epoch'], 2)):
            x, y = batch['image'].cuda(), batch['semantic_mask'].cuda()
            gain = .9+.2*torch.rand((len(x),1,1,1), device='cuda')
            gamma = .9+.2*torch.rand((len(x),1,1,1), device='cuda')
            x = (x*gain).clamp(0,1).pow(gamma)
            before = torch.cuda.get_rng_state()
            with torch.autocast('cuda', dtype=torch.float16): logits = model(x); loss = loss_value(logits,y,config['loss'])
            if not torch.isfinite(loss):
                trace = []
                def hook(name):
                    def record(module, inputs, output):
                        if torch.is_tensor(output) and not torch.isfinite(output).all():
                            maximum = float(inputs[0].detach().abs().max())
                            trace.append({'module': name, 'class': type(module).__name__,
                                          'input_max': maximum if math.isfinite(maximum) else str(maximum)})
                    return record
                handles = [m.register_forward_hook(hook(n)) for n,m in model.named_modules()
                           if isinstance(m,(torch.nn.Conv2d,torch.nn.ELU,torch.nn.ConvTranspose2d))]
                torch.cuda.set_rng_state(before)
                with torch.autocast('cuda',dtype=torch.float16): model(x)
                for handle in handles: handle.remove()
                torch.cuda.set_rng_state(before)
                with torch.autocast('cuda',enabled=False): full = model(x.float()); full_loss = loss_value(full,y,config['loss'])
                from mdva.training.cnn193_fp32 import architecture as repaired
                repaired_model, _ = repaired({**config, 'cnn193_compute': 'fp32'})
                repaired_model.load_state_dict(model.state_dict()); repaired_model.cuda().train()
                torch.cuda.set_rng_state(before)
                with torch.autocast('cuda', dtype=torch.float16):
                    repaired_logits = repaired_model(x); repaired_loss = loss_value(repaired_logits,y,config['loss'])
                repaired_loss.backward()
                result = {'diagnostic_only': True, 'epoch': saved['epoch']+1, 'step': step,
                          'uri': batch['uri'], 'input_finite': bool(torch.isfinite(x).all()),
                          'nonfinite_weights': [k for k,v in model.state_dict().items() if not torch.isfinite(v).all()],
                          'first_bad': trace[:5], 'fp32_logits_finite': bool(torch.isfinite(full).all()),
                          'fp32_loss': float(full_loss.detach()),
                          'adapter_logits_finite': bool(torch.isfinite(repaired_logits).all()),
                          'adapter_gradients_finite': all(p.grad is None or bool(torch.isfinite(p.grad).all()) for p in repaired_model.parameters())}
                atomic_json(exp/'ch-numerical-failure-probe.json',result)
                print(json.dumps(result,ensure_ascii=False),flush=True)
                return
            window_start = (step//4)*8; window = min(8,len(dataset)-window_start)
            scaler.scale(loss*len(x)/window).backward()
            if (step+1)%4 == 0 or (step+1)*2 >= len(dataset):
                scaler.step(optimizer);scaler.update();optimizer.zero_grad(set_to_none=True)
            if step%200==0: print('replay step',step,flush=True)
        raise RuntimeError('未复现，不能宣称已确认根因')


if __name__ == '__main__': main()
