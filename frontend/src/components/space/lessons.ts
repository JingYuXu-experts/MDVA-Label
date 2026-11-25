import { earthPosition, usableAnalysis, type SceneEvent } from './sceneMath'
import { decimalYear, dipoleMoment, protonDynamicPressure, type Inspection, type Topic, type WindSample } from './physics'
import type { MagneticModel, SolarImage } from './products'

export interface LessonContext { time: number; now: number; timeScale: number; event: SceneEvent | null; wind: WindSample; hit?: Inspection | null; magnetic?:MagneticModel; solarImage?:SolarImage }
export interface Lesson { title: string; explanation: string; state: string[]; limits: string; source: string; url: string }
const stamp = (t: number) => new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC'

/** Every annotation separates a model value, an observation and an unavailable field. */
export function lesson(topic: Topic, context: LessonContext): Lesson {
  const { time, now, timeScale, event, wind, hit } = context
  const clock = `场景时间：${stamp(time)}；${timeScale === 1 ? '实时速率' : `教学加速 ${timeScale.toLocaleString()}×，不是未来预报`}。`
  if(topic==='sun'&&context.solarImage)return {
    title:'太阳活动 · 有时间的实际观测',explanation:'图像来自SDO或SOHO的单视角观测。AIA 304可查看日缘日珥和暗条，171与193提供不同温度响应的日冕结构。明亮区域本身不能直接确认为耀斑。',
    state:[`通道 ${context.solarImage.channel}；实际观测 ${context.solarImage.observed_at}。`,'三维场景将影像投影到观测可见半球，背面用未知网格表示；日缘发射保留在观测平面；可在太阳活动观测页放大、逐帧回放。','影像时间独立于天文教学加速，没有将静态一帧伪装成未来演化。'],limits:'单视角无法给出日珥深度或背面活动；没有把平面影像称为三维重建。',source:'Helioviewer / NASA SDO',url:context.solarImage.source_url,
  }
  if (topic === 'sun') return {
    title: '太阳 · 较差自转', explanation: '太阳是等离子体，不像固体球一样处处同速自转。赤道附近转得较快，两极附近较慢。太阳在本日心坐标系中位于原点，地球围绕它公转。',
    state: [clock, '表面使用NASA给出的纬度角速度参考律：赤道14.37°/天，两极10.48°/天；自转轴按天文模型计算。', '当前表面是静态纹理随参考律转动，没有从这张纹理判断耀斑、黑子或太阳风暴。'],
    limits: '不是实时太阳表面重建；未纳入太阳绕银河系的运动。亮度光晕仅为显示效果。', source: 'NASA Sun Fact Sheet', url: 'https://nssdc.gsfc.nasa.gov/planetary/factsheet/sunfact.html',
  }
  if (topic === 'earth') return {
    title: '地球 · 自转与公转', explanation: '地球自转形成昼夜，绕太阳公转形成年度轨道运动。自转轴的倾斜参与形成季节。自转和公转共用一个物理时钟，不能为好看而随意改变两者速度比。',
    state: [clock, `天文计算的当前日地距离：${Math.hypot(...earthPosition(time)).toFixed(6)} AU。`, '姿态使用格林尼治恒星时与岁差、章动坐标变换。日夜纹理按太阳照明显示；云层与城市灯光是静态材质。'],
    limits: '这是天文模型计算，不是地球实时卫星视频。增强显示放大了天体尺寸；可切换真实比例。', source: 'NASA 地球资料 / Astronomy Engine', url: 'https://science.nasa.gov/earth/facts/',
  }
  if (topic === 'field') {
    const model=context.magnetic
    if(model)return {
      title:'磁层场 · T96 + IGRF-14',explanation:'磁力线沿内源场与磁层电流外源场之和进行数值积分，包含磁尾和不对称结构。半透明外罩是Shue 1998独立磁层顶经验估计，不是磁场终止的位置。',
      state:[`模型时间 ${stamp(model.parameters.time)}；${model.driver_mode==='manual'?'用户手动条件实验':'L1 / Dst观测驱动快照'}。`,`动压 ${model.parameters.pressure.toFixed(3)} nPa；Dst ${model.parameters.dst} nT；IMF By ${model.parameters.by}、Bz ${model.parameters.bz} nT。`,`Shue迎日点 ${model.boundary.r0.toFixed(2)} Rₑ；磁力线绘制域X −50～20 Rₑ。`,hit?.magnitudeNt!==undefined?`所选磁力线采样点约 ${hit.magnitudeNt.toFixed(1)} nT，鼠标距地心 ${hit.radiusRe?.toFixed(2)} Rₑ。`:model.driver_note,...model.warnings],
      limits:'这是瞬时经验模型，不能代替MHD时序、不能直接显示磁重联或冲击波。两种模型独立计算，边界和场线可能不完全一致。数值与场景天文时间分别标识。',source:'NASA CCMC / Tsyganenko T96；Shue 1998',url:'https://ccmc.gsfc.nasa.gov/models/Tsyganenko%20Magnetic%20Field~T96/',
    }
    const moment = dipoleMoment(time)
    return {
      title: '地球内源磁场 · IGRF-14一阶分量', explanation: '这些线表示地磁场的方向，不是实体管道。这里只画官方IGRF-14的一阶内源偶极分量；实际地磁场还包括更高阶分量和空间电流的影响。',
      state: [clock, moment ? `模型年：${decimalYear(time).toFixed(3)}；参考赤道强度约${(Math.hypot(...moment)/1000).toFixed(2)} µT。` : '当前时间超出2025—2030年适用窗口，未外推磁场。', hit?.magnitudeNt !== undefined ? `鼠标位置：距地心${hit.radiusRe?.toFixed(2)} Rₑ，一阶模型强度${hit.magnitudeNt.toFixed(0)} nT。` : '本图绘制1—3 Rₑ；1 Rₑ=6371.2 km。这是绘制范围，不是磁场终止的位置。'],
      limits: '没有模拟磁层顶、磁尾、弓激波或太阳风压缩，不能从这些线判断当前磁暴或保护范围。', source: 'NOAA/NCEI IGRF-14官方系数', url: 'https://www.ncei.noaa.gov/products/international-geomagnetic-reference-field',
    }
  }
  if (topic === 'wind') {
    const age = wind.observedAt === undefined ? Infinity : now - wind.observedAt
    const fresh = age >= -120 && age <= 900
    const pressure = fresh ? protonDynamicPressure(wind) : null
    return {
      title: '太阳风 · 持续外流的等离子体', explanation: '太阳风包含带电粒子和磁场，持续从太阳向外流动。粒子运动与磁场线形状是两件事，不能把螺旋磁场线直接当作粒子飞行轨迹。',
      state: [wind.observedAt ? `上游监测时间：${stamp(wind.observedAt)}；${fresh?'在15分钟时效内':'已过期，不能当成当前状态'}。` : '尚无可用上游监测时间。', fresh ? `L1附近速度${wind.speed ?? '缺测'} km/s；质子密度${wind.density ?? '缺测'} cm⁻³；Bz ${wind.bz ?? '缺测'} nT。` : '没有足够新鲜数据，不生成实时正常结论。', pressure === null ? '未计算动压。' : `仅质子动压 mₚnv²≈${pressure.toFixed(3)} nPa，未包含α粒子等贡献。`],
      limits: '金色长线和箭头是定常径向参考示踪，各方向向外；仅在L1数据新鲜时使用其速度作参考，缺测时只标方向。线条数量不是粒子密度，也不是完整日冕流场。进入磁层附近后停止这种参考示踪，未伪造绕流、冲击或重联。实际相互作用请看磁层MHD实况模型。', source: 'NOAA 太阳风说明 / RTSW', url: 'https://www.spaceweather.gov/phenomena/solar-wind',
    }
  }
  if (topic === 'cme') {
    const a = usableAnalysis(event)
    return {
      title: 'CME · 日冕物质抛射', explanation: 'CME是向一定方向膨胀、传播的等离子体及磁结构。它不一定朝地球传播，也不是规则圆盘或一圈圈实体阵列。',
      state: [event ? `选中事件：${event.activityID}；开始${event.startTime}。` : '尚未选择来源事件。', a ? `来源参数：${a.speed} km/s，HEEQ经度${a.longitude}°、纬度${a.latitude}°、半角${a.halfAngle}°。` : '没有完整且合法的速度、方向、半角参数。', '紫色箭头将来源HEEQ方向按分析时刻的太阳极轴与地球参考子午面转换到三维空间；箭头不随当前地球位置重新瞄准。它只标来源方向，不表示真实CME形态或冲击面。'],
      limits: '速度、方向和半角不能唯一确定三维密度、磁场、形状或未来冲击过程。需额外模型网格、坐标及观测验证。', source: 'NASA DONKI / NOAA CME说明', url: 'https://www.spaceweather.gov/phenomena/coronal-mass-ejections',
    }
  }
  return {
    title: '冲击波与地球弓激波', explanation: 'CME驱动的行星际冲击波，与太阳风绕地球磁层形成的弓激波不是同一个对象。并非每个CME都会形成冲击波，更不能把一个圆盘默认画成冲击波。',
    state: [clock, '尚未接入可验证的三维密度、速度、磁场和激波判据，因此当前没有生成冲击波网格，也没有宣布它已经抵达地球。', '可以切换官方二维传播模型查看已有模型产品；原始观测与模型预报分别标识。'],
    limits: '必须有相应模型输出或观测证据才能显示其位置和状态，不能由动画进度触发“冲击发生”。', source: 'NASA / NOAA 磁层与弓激波资料', url: 'https://www.spaceweather.gov/phenomena/earths-magnetosphere',
  }
}
