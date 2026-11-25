import type { MagneticModel } from './products'
import type { WindSample } from './physics'
import { earthImpact, upstreamIntake, type SolarEvent, type PropagationModel } from './impact.ts'

export type GuideStep = 0 | 1 | 2 | 3
export interface ModelStatus { state: 'idle'|'running'|'ready'|'error'; message?:string }
export const guideSteps=['认位置','看太阳风','看地球磁场','查喷发影响'] as const
export function guideContent(step:GuideStep,context:{now:number;wind:WindSample;magnetic?:MagneticModel;modelStatus:ModelStatus;event:SolarEvent|null;simulations:PropagationModel[]}){
  const {now,wind,magnetic,event,simulations,modelStatus}=context
  const sample=upstreamIntake(wind,now)
  if(step===0)return {
    title:'先找到太阳，再找到我们所在的地球',
    description:'这张图说明太阳与地球的空间关系。先认识两个位置，再看从太阳吹出来的东西怎样与地球周围的磁场相互作用。',
    look:'先看标着“太阳”和“地球”的两个球；按住左键可以转动视角。',
    state:'当前只显示位置关系；天体尺寸放大以便看清。',
    limit:'这不是一场太阳风暴的直播，也没有据此判断当前有危险。',kind:'空间示意',next:'下一步：看太阳风',
  }
  if(step===1)return {
    title:'金色箭头表示：太阳风持续向外吹',
    description:'太阳风是太阳持续释放的带电粒子流，也流向地球附近。它一直存在；看到太阳风并不等于发生了太阳风暴。',
    look:'顺着太阳旁的金色箭头看向地球；其他方向也有太阳风。',
    state:sample.valid?`地球前方实测：速度约 ${wind.speed!.toFixed(0)} km/s，质子密度约 ${wind.density!.toPrecision(3)} 个/cm³。`:'地球前方测量缺失、过期或不匹配；目前只展示流向。',
    limit:'箭头是方向示意，不是逐个拍到的粒子；多少根线不代表实际密度。',kind:sample.valid?'实测数值 + 方向示意':'方向示意 · 实测不可用',next:'下一步：看地球磁场',
  }
  if(step===2)return {
    title:'地球周围的线，表示磁场的方向',
    description:magnetic?'现在镜头来到地球旁：朝太阳的一侧较紧凑，背向太阳的一侧延伸成磁尾。这种形态与太阳风和地球磁场的相互作用有关。':'现在先看地球内源磁场。全局模型取得后，才能查看迎日侧与磁尾形态。',
    look:magnetic?'亮曲线是磁力线，很淡的外框是边界估计；都不是实体管道。':'找到地球标签；当前曲线只表示内源磁场参考分量。',
    state:magnetic?`${magnetic.driver_mode==='manual'?'手动条件实验':'观测驱动模型'} · 快照 ${new Date(magnetic.parameters.time*1000).toISOString().replace('T',' ').slice(0,16)} UTC。${modelStatus.state==='error'?'更新失败，保留此快照；'+(modelStatus.message||'请重试'):modelStatus.state==='running'?'正在更新，当前仍显示此快照。':''}`:modelStatus.state==='error'?'全局模型未取得：'+(modelStatus.message||'驱动数据不可用'):'正在准备全局磁场模型，暂不把内源场当成完整磁层。',
    limit:'这是一张模型快照，不是某次喷发撞击地球的录像；磁场也不是完全封闭的保护壳。',kind:magnetic?'磁场模型快照':'内源场参考 · 全局模型未就绪',next:'下一步：查具体喷发',
  }
  const impact=earthImpact(event,simulations,now)
  return {
    title:event?'这次喷发会不会影响地球？看来源给出的判断':'要判断影响，先选择一次真实喷发',
    description:'CME是一次太阳物质与磁结构的抛射。选中事件后，紫色箭头标出来源分析的方向；地球到达估计和影响解读显示在信息卡。',
    look:'先选喷发事件，再看“地球到达”与“资料是否齐全”；不要凭箭头看起来接近地球就判断命中。',
    state:impact.title+'。'+impact.detail,
    limit:'当前没有重建CME三维冲击过程；拖动时间不能用来宣布已经到达地球。',kind:'事件研究资料',next:'从头再看一遍',
  }
}
