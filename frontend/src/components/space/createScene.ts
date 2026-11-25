import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js'
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js'
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js'
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js'
import { earthPosition, WORLD_AU, usableAnalysis, type SceneEvent } from './sceneMath'
import { earthBasis, solarNorth, dipoleMoment, dipoleField, heeqDirection, type Inspection } from './physics'
import { sphereVertex, sunFragment, earthFragment, atmosphereFragment, coronaFragment,solarLimbFragment } from './sceneShaders'
import sunUrl from '@/assets/space/sun.jpg'
import dayUrl from '@/assets/space/earth-day.jpg'
import nightUrl from '@/assets/space/earth-night.jpg'
import cloudsUrl from '@/assets/space/earth-clouds.jpg'
import specUrl from '@/assets/space/earth-specular.jpg'
import { magneticGeometry, gsmPoint } from './magneticGeometry'
import type { MagneticModel, SolarImage } from './products'
import type { WindSample } from './physics'

export type CameraPreset = 'overview' | 'earth' | 'sun' | 'top' | 'field'
export interface SceneState {
  time: number
  event: SceneEvent | null
  grid: boolean
  realisticScale: boolean
  quality: 'high' | 'balanced'
  field: boolean
  magnetic?: MagneticModel
  wind?: WindSample
  flow?: boolean
  solarImage?: SolarImage
  trainingActive?: boolean
  visible?: boolean
  guided?: boolean
}
export interface LabelPosition { x: number; y: number; visible: boolean }
export interface RenderStats { fps: number; sun: LabelPosition; earth: LabelPosition; distance: number | null; orbits: {au: number; position: LabelPosition}[]; solarObserved:boolean;fieldModel:string;fieldLines:number;windTracers:number;fieldSides?:{sunward:LabelPosition;tailward:LabelPosition} }

/** Owned GPU resources: one renderer per mounted studio, no network requests. */
export function createScene(host: HTMLElement, state: () => SceneState, report: (stats: RenderStats) => void, fail: (message: string) => void, inspect: (value: Inspection | null, pin?: boolean) => void = () => {}) {
  const scene = new THREE.Scene()
  scene.background = new THREE.Color('#02050a')
  const camera = new THREE.PerspectiveCamera(42, 1, .008, 600)
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
  renderer.setPixelRatio(Math.min(devicePixelRatio, state().trainingActive ? 1 : 1.75))
  renderer.toneMapping = THREE.ACESFilmicToneMapping
  renderer.toneMappingExposure = 1.05
  host.appendChild(renderer.domElement)
  renderer.domElement.setAttribute('aria-label', '可旋转缩放的三维日地传播场景')
  renderer.domElement.addEventListener('webglcontextlost', contextLost)
  const controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = .07
  controls.minDistance = .1
  controls.maxDistance = 65
  controls.maxPolarAngle = Math.PI * .94
  controls.target.set(5.3, 0, 0)
  const composer = new EffectComposer(renderer)
  composer.addPass(new RenderPass(scene, camera))
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), .48, .55, 1.05)
  composer.addPass(bloom)
  composer.addPass(new OutputPass())

  const loader = new THREE.TextureLoader()
  const textures: THREE.Texture[] = []
  let disposed = false
  function texture(url: string, color = true) {
    const loaded = loader.load(url, () => { if (disposed) loaded.dispose() }, undefined, () => fail('部分本地纹理加载失败，请刷新或检查应用构建产物。'))
    if (color) loaded.colorSpace = THREE.SRGBColorSpace
    loaded.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy())
    textures.push(loaded)
    return loaded
  }
  const solarMaterial = new THREE.ShaderMaterial({ vertexShader: sphereVertex, fragmentShader: sunFragment,
    uniforms: { surface: { value: texture(sunUrl) }, time: { value: 0 }, rotationDays: {value: 0},observedMap:{value:null},hasObservation:{value:0},observedRadius:{value:.394},observedCenter:{value:new THREE.Vector2(.5,.5)},observerAxis:{value:new THREE.Vector3(1,0,0)},observerUp:{value:new THREE.Vector3(0,1,0)},observerRight:{value:new THREE.Vector3(0,0,-1)} } })
  const sun = new THREE.Mesh(new THREE.SphereGeometry(1, 128, 96), solarMaterial)
  sun.scale.setScalar(1.35)
  scene.add(sun)
  const coronaMaterial = new THREE.ShaderMaterial({
    vertexShader: sphereVertex, fragmentShader: coronaFragment, transparent: true, depthWrite: false,
    blending: THREE.AdditiveBlending, uniforms: { time: { value: 0 } },
  })
  const corona = new THREE.Mesh(new THREE.PlaneGeometry(10, 10), coronaMaterial)
  scene.add(corona)
  // An observer-oriented image plane preserves the actual off-limb emission.
  // It is never wrapped over the unobserved far hemisphere as a fabricated map.
  const observedMaterial=new THREE.ShaderMaterial({vertexShader:sphereVertex,fragmentShader:solarLimbFragment,uniforms:{surface:{value:null},radius:{value:.394},center:{value:new THREE.Vector2(.5,.5)}},transparent:true,depthWrite:false,blending:THREE.AdditiveBlending,side:THREE.FrontSide,toneMapped:false})
  const observedSun=new THREE.Mesh(new THREE.PlaneGeometry(1,1),observedMaterial)
  observedSun.userData.topic='sun';observedSun.visible=false;scene.add(observedSun)
  let solarImageId='',solarGeneration=0
  let solarTexture:THREE.Texture|undefined
  let observation:SolarImage|undefined
  function solarObservation(image:SolarImage){
    solarImageId=image.id;const generation=++solarGeneration
    loader.load('/api/sw/assets?id='+encodeURIComponent(image.id),loaded=>{
      if(disposed||generation!==solarGeneration){loaded.dispose();return}
      loaded.colorSpace=THREE.SRGBColorSpace;loaded.anisotropy=Math.min(8,renderer.capabilities.getMaxAnisotropy())
      solarTexture?.dispose();solarTexture=loaded;observedMaterial.uniforms.surface!.value=loaded;solarMaterial.uniforms.observedMap!.value=loaded;solarMaterial.uniforms.hasObservation!.value=1;observation=image
    },undefined,()=>{if(generation===solarGeneration)fail('太阳观测影像加载失败，可在太阳活动观测中重试。')})
  }

  const earthGroup = new THREE.Group()
  // The Earth-axis tilt is a presentation approximation in the ecliptic scene.
  const axialTilt = new THREE.Group()
  earthGroup.add(axialTilt)
  const earthMaterial = new THREE.ShaderMaterial({vertexShader: sphereVertex, fragmentShader: earthFragment,
    uniforms: { dayMap: {value: texture(dayUrl)}, nightMap: {value: texture(nightUrl)}, specMap: {value: texture(specUrl, false)} }})
  const earth = new THREE.Mesh(new THREE.SphereGeometry(1, 128, 96), earthMaterial)
  axialTilt.add(earth)
  const cloudTexture = texture(cloudsUrl, false)
  const clouds = new THREE.Mesh(new THREE.SphereGeometry(1.012, 96, 64), new THREE.MeshPhongMaterial({
    map: cloudTexture, alphaMap: cloudTexture, color: 0xeaf6ff, transparent: true, opacity: .65, depthWrite: false,
  }))
  axialTilt.add(clouds)
  const atmosphere = new THREE.Mesh(new THREE.SphereGeometry(1.065, 96, 64), new THREE.ShaderMaterial({
    vertexShader: sphereVertex, fragmentShader: atmosphereFragment, transparent: true, depthWrite: false,
    blending: THREE.AdditiveBlending, side: THREE.BackSide,
  }))
  axialTilt.add(atmosphere)
  earthGroup.scale.setScalar(.53)
  scene.add(earthGroup)
  const globalField=new THREE.Group();earthGroup.add(globalField)
  let magneticSnapshot:MagneticModel|undefined
  // Radial steady-flow tracers illustrate outward direction. They are not a
  // reconstruction of the global velocity field or measured particle positions.
  const flowGeometry=new THREE.BufferGeometry(),flowPositions=new Float32Array(72*18)
  flowGeometry.setAttribute('position',new THREE.BufferAttribute(flowPositions,3))
  const flowLines=new THREE.LineSegments(flowGeometry,new THREE.LineBasicMaterial({color:0xf4b760,transparent:true,opacity:.48,depthWrite:false}))
  flowLines.userData.topic='wind';scene.add(flowLines)
  const flowDirections=Array.from({length:72},(_,i)=>{const y=1-2*(i+.5)/72,a=i*2.399963229728653;return new THREE.Vector3(Math.cos(a)*Math.sqrt(1-y*y),y,Math.sin(a)*Math.sqrt(1-y*y))})
  const windPathsGeometry=new THREE.BufferGeometry(),windPathsPositions=new Float32Array(72*6)
  windPathsGeometry.setAttribute('position',new THREE.BufferAttribute(windPathsPositions,3))
  const windPaths=new THREE.LineSegments(windPathsGeometry,new THREE.LineBasicMaterial({color:0xe5af62,transparent:true,opacity:.16,depthWrite:false}))
  windPaths.userData.topic='wind';scene.add(windPaths)
  const cmeArrow=new THREE.ArrowHelper(new THREE.Vector3(1,0,0),new THREE.Vector3(),3,0xd89cfa,.42,.2)
  cmeArrow.userData.topic='cme';cmeArrow.visible=false;scene.add(cmeArrow)
  cmeArrow.children.forEach(child=>child.userData.topic='cme')
  const windArrow=new THREE.ArrowHelper(new THREE.Vector3(1,0,0),new THREE.Vector3(1.6,0,0),4,0xf3b45f,.28,.12);scene.add(windArrow)
  const upstreamArrows=new THREE.Group();earthGroup.add(upstreamArrows)
  for(let i=0;i<12;i++){
    const point=new THREE.Vector3(22+(i%3)*5,((i%4)-1.5)*7,((i%2)-.5)*12)
    const arrow=new THREE.ArrowHelper(new THREE.Vector3(-1,0,0),point,3,0xe1a65b,1,.5)
    arrow.userData.topic='wind';upstreamArrows.add(arrow)
  }
  const light = new THREE.PointLight(0xfff3df, 130, 0, 1)
  scene.add(light)
  scene.add(new THREE.AmbientLight(0x224469, .12))

  const grid = new THREE.Group()
  function line(points: THREE.Vector3[], color: number, opacity: number) {
    return new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color, transparent: true, opacity }))
  }
  for (const au of [.25, .5, .75, 1, 1.25, 1.5]) {
    const points = Array.from({ length: 257 }, (_, i) => new THREE.Vector3(Math.cos(i / 256 * Math.PI * 2) * WORLD_AU * au, -.015, Math.sin(i / 256 * Math.PI * 2) * WORLD_AU * au))
    grid.add(line(points, au === 1 ? 0x577887 : 0x254356, au === 1 ? .45 : .3))
  }
  for (let i = 0; i < 24; i++) {
    const angle = i / 24 * Math.PI * 2
    grid.add(line([new THREE.Vector3(0, -.015, 0), new THREE.Vector3(Math.cos(angle) * 18, -.015, Math.sin(angle) * 18)], 0x183142, .32))
  }
  scene.add(grid)
  const starsGeometry = new THREE.BufferGeometry()
  const stars = new Float32Array(2700)
  // Decorative fixed star background; deliberately not an astrometric sky catalog.
  for (let i = 0; i < 900; i++) {
    const theta = i * 2.399963
    const y = 1 - 2 * ((i + .5) / 900)
    const radius = Math.sqrt(1 - y * y)
    stars.set([Math.cos(theta) * radius * 180, y * 180, Math.sin(theta) * radius * 180], i * 3)
  }
  starsGeometry.setAttribute('position', new THREE.BufferAttribute(stars, 3))
  scene.add(new THREE.Points(starsGeometry, new THREE.PointsMaterial({color: 0x96a6bf, size: .13, transparent: true, opacity: .55, sizeAttenuation: true})))

  // Only a verified internal-field component is drawn. A finite magnetopause
  // or CME shock cannot be derived from a decorative sphere or one L1 sample.
  const fieldGroup = new THREE.Group()
  axialTilt.add(fieldGroup)
  let fieldEpoch = ''
  function rebuildField(seconds: number) {
    fieldGroup.traverse(object => {
      const drawable = object as THREE.Line
      drawable.geometry?.dispose()
      if (drawable.material instanceof THREE.Material) drawable.material.dispose()
    })
    fieldGroup.clear()
    const moment = dipoleMoment(seconds)
    if (!moment) return
    const pole = new THREE.Vector3(moment[0], moment[2], -moment[1]).normalize()
    const rotation = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), pole)
    for (const shell of [1.3, 1.7, 2.2, 3]) for (let azimuth = 0; azimuth < 12; azimuth++) {
      const phi = azimuth / 12 * Math.PI * 2
      const start = Math.asin(Math.sqrt(1 / shell))
      const positions: THREE.Vector3[] = []
      const colors: number[] = []
      for (let i = 0; i <= 100; i++) {
        const theta = start + (Math.PI - 2 * start) * i / 100
        // Exact centered-dipole field-line relation r = L sin²(theta).
        const radius = shell * Math.sin(theta) ** 2
        const point = new THREE.Vector3(radius * Math.sin(theta) * Math.cos(phi), radius * Math.cos(theta), radius * Math.sin(theta) * Math.sin(phi)).applyQuaternion(rotation)
        const field = dipoleField([point.x, -point.z, point.y], seconds)
        const strength = field ? Math.hypot(...field) : 0
        const color = new THREE.Color().setHSL(.58 - Math.min(1, Math.max(0, Math.log10(Math.max(strength, 500) / 500) / 2)) * .45, .72, .6)
        positions.push(point); colors.push(color.r, color.g, color.b)
      }
      const geometry = new THREE.BufferGeometry().setFromPoints(positions)
      geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3))
      const curve = new THREE.Line(geometry, new THREE.LineBasicMaterial({vertexColors: true, transparent: true, opacity: .42}))
      curve.userData.topic = 'field'
      fieldGroup.add(curve)
    }
  }
  sun.userData.topic = 'sun'
  earth.userData.topic = 'earth'
  const raycaster = new THREE.Raycaster()
  raycaster.params.Line = {threshold: .025}
  let dragging = false
  let lastPress: {x: number; y: number} | null = null
  controls.addEventListener('start', () => { dragging = true; inspect(null) })
  controls.addEventListener('end', () => { dragging = false })
  function pick(event: MouseEvent, pin = false) {
    if (dragging) return
    const box = renderer.domElement.getBoundingClientRect()
    const x = event.clientX - box.left, y = event.clientY - box.top
    raycaster.setFromCamera(new THREE.Vector2(x / width * 2 - 1, 1 - y / height * 2), camera)
    const targets: THREE.Object3D[] = [sun, earth]
    if(observedSun.visible)targets.push(observedSun)
    if (fieldGroup.visible) targets.push(...fieldGroup.children)
    if(globalField.visible)targets.push(...globalField.children.filter(o=>o instanceof THREE.Line))
    if(flowLines.visible)targets.push(flowLines)
    if(windPaths.visible)targets.push(windPaths)
    if(cmeArrow.visible)targets.push(...cmeArrow.children)
    const hit = raycaster.intersectObjects(targets, false)[0]
    if (!hit) { if (!pin) inspect(null); return }
    const topic = hit.object.userData.topic as Inspection['topic']
    if(!topic){if(!pin)inspect(null);return}
    const value: Inspection = {topic, x, y}
    if (topic === 'field') {
      const local = (magneticSnapshot?globalField:axialTilt).worldToLocal(hit.point.clone())
      value.radiusRe = local.length()
      if(magneticSnapshot){const strength=(hit.object as THREE.Line).geometry?.getAttribute('strength');if(strength&&hit.index!==undefined)value.magnitudeNt=strength.getX(hit.index)}
      else{const vector = dipoleField([local.x, -local.z, local.y], state().time);if (vector) value.magnitudeNt = Math.hypot(...vector)}
    }
    inspect(value, pin)
  }
  const move = (event: PointerEvent) => pick(event)
  const down = (event: PointerEvent) => { lastPress = {x: event.clientX, y: event.clientY} }
  const click = (event: MouseEvent) => {
    if (lastPress && Math.hypot(event.clientX-lastPress.x,event.clientY-lastPress.y)>8) return
    pick(event, true)
  }
  renderer.domElement.addEventListener('pointermove', move)
  renderer.domElement.addEventListener('pointerdown', down)
  renderer.domElement.addEventListener('click', click)

  const reference = earthPosition(state().time)
  const referenceAngle = Math.atan2(reference[2], reference[0])
  function earthAt(seconds: number) {
    const [x, y, z] = earthPosition(seconds)
    return new THREE.Vector3(x * Math.cos(referenceAngle) + z * Math.sin(referenceAngle), y, -x * Math.sin(referenceAngle) + z * Math.cos(referenceAngle))
  }
  let trackingEarth = false
  let activePreset:CameraPreset='overview'
  const lastEarth = new THREE.Vector3()
  let cameraTransition: {start: number; from: THREE.Vector3; to: THREE.Vector3; targetFrom: THREE.Vector3; targetTo: THREE.Vector3} | null = null
  function preset(name: CameraPreset, smooth = true) {
    activePreset=name
    trackingEarth = name === 'earth' || name === 'field'
    lastEarth.copy(earthAt(state().time).multiplyScalar(WORLD_AU))
    const ep = earthAt(state().time).multiplyScalar(WORLD_AU)
    const guided=!!state().guided,aspect=Math.max(.25,host.clientWidth/Math.max(1,host.clientHeight))
    const center=ep.clone().multiplyScalar(.5)
    const overviewDistance=Math.max(24,(ep.length()+4)/2/(Math.tan(camera.fov*Math.PI/360)*aspect))
    const fieldOffset=new THREE.Vector3(2.2,2.6,7.8).multiplyScalar(guided?Math.max(1,1/aspect):1)
    const positions: Record<CameraPreset, [THREE.Vector3, THREE.Vector3]> = {
      overview: guided?[center.clone().add(new THREE.Vector3(0,overviewDistance*.34,overviewDistance*.94)),center]:[new THREE.Vector3(2.6, 10, 26), new THREE.Vector3(2.4, 0, 0)],
      field: [ep.clone().add(fieldOffset), ep.clone().add(new THREE.Vector3(1.4,0,0))],
      earth: [ep.clone().add(new THREE.Vector3(-1.1, .9, 2.1)).lerp(ep,magneticSnapshot?.82:0), ep],
      sun: [ep.clone().normalize().multiplyScalar(5.5).add(new THREE.Vector3(0,.1,.1)), new THREE.Vector3()],
      top: [new THREE.Vector3(5, 20, .01), new THREE.Vector3(5, 0, 0)],
    }
    const [position, target] = positions[name]
    if (!smooth) {cameraTransition=null;camera.position.copy(position); controls.target.copy(target)}
    else cameraTransition = {start: performance.now(), from: camera.position.clone(), to: position, targetFrom: controls.target.clone(), targetTo: target}
  }
  controls.addEventListener('start', () => { cameraTransition = null })
  preset('overview', false)

  let frame = 0
  let lastReport = performance.now()
  let count = 0
  let priorTime = NaN
  let lastRender = 0
  const earthVector = new THREE.Vector3()
  let width = 1, height = 1
  let lastGuided:boolean|undefined
  function resize() {
    width = Math.max(1, host.clientWidth); height = Math.max(1, host.clientHeight)
    lastGuided=!!state().guided
    if(lastGuided){camera.setViewOffset(width,height,0,height*.1,width,height);preset(activePreset,false)}else camera.clearViewOffset()
    camera.aspect = width / height; camera.updateProjectionMatrix()
    renderer.setSize(width, height, false); composer.setSize(width, height)
  }
  const observer = new ResizeObserver(resize)
  observer.observe(host)
  function project(vector: THREE.Vector3): LabelPosition {
    const p = vector.clone().project(camera)
    return {x: (p.x + 1) * width / 2, y: Math.max(155, Math.min(height - 150, (-p.y + 1) * height / 2)), visible: p.z < 1 && p.z > -1 && Math.abs(p.x) < 1 && Math.abs(p.y) < 1}
  }
  function animate(now: number) {
    if (disposed) return
    frame = requestAnimationFrame(animate)
    const data = state()
    if (document.hidden || data.visible === false || now - lastRender < (data.trainingActive ? 1000 / 15 : data.quality === 'high' ? 1000 / 60 : 1000 / 30)) return
    lastRender = now
    if(!!data.guided!==lastGuided)resize()
    if(data.magnetic&&data.magnetic!==magneticSnapshot){magneticSnapshot=data.magnetic;magneticGeometry(globalField,data.magnetic)}
    if(data.solarImage&&data.solarImage.id!==solarImageId)solarObservation(data.solarImage)
    if (data.time !== priorTime) {
      earthVector.copy(earthAt(data.time).multiplyScalar(WORLD_AU))
      earthGroup.position.copy(earthVector)
      const basis = earthBasis(data.time)
      const intoScene = (value: [number, number, number]) => new THREE.Vector3(...value).applyAxisAngle(new THREE.Vector3(0, 1, 0), referenceAngle)
      axialTilt.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(intoScene(basis.x), intoScene(basis.north), intoScene(basis.z)))
      const north = intoScene(solarNorth(data.time)).normalize()
      sun.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), north)
      solarMaterial.uniforms.rotationDays!.value = (data.time - 946728000) / 86400
      if (trackingEarth && !cameraTransition) {
        const delta = earthVector.clone().sub(lastEarth)
        camera.position.add(delta); controls.target.add(delta)
      }
      lastEarth.copy(earthVector)
      const epoch = new Date(data.time * 1000).toISOString().slice(0, 10)
      if (epoch !== fieldEpoch) { rebuildField(data.time); fieldEpoch = epoch }
      priorTime = data.time
    }
    if(magneticSnapshot){
      const basis=earthBasis(magneticSnapshot.parameters.time)
      const geoToScene=(p:[number,number,number])=>new THREE.Vector3(...basis.x).multiplyScalar(p[0]).addScaledVector(new THREE.Vector3(...basis.north),p[2]).addScaledVector(new THREE.Vector3(...basis.z),-p[1]).applyAxisAngle(new THREE.Vector3(0,1,0),referenceAngle)
      const x=geoToScene(magneticSnapshot.basis_geo[0]!),y=geoToScene(magneticSnapshot.basis_geo[2]!),z=geoToScene(magneticSnapshot.basis_geo[1]!).negate()
      globalField.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(x,y,z));upstreamArrows.quaternion.copy(globalField.quaternion)
      // ArrowHelper uses native XYZ, so transform each GSM vector consistently.
      upstreamArrows.children.forEach((object,i)=>{object.position.copy(gsmPoint([22+(i%3)*5,((i%4)-1.5)*7,((i%2)-.5)*12]));(object as THREE.ArrowHelper).setDirection(new THREE.Vector3(-1,0,0))})
    }
    if (cameraTransition) {
      const amount = Math.min(1, (now - cameraTransition.start) / 1000)
      const easing = amount * amount * (3 - 2 * amount)
      camera.position.lerpVectors(cameraTransition.from, cameraTransition.to, easing)
      controls.target.lerpVectors(cameraTransition.targetFrom, cameraTransition.targetTo, easing)
      if (amount === 1) cameraTransition = null
    }
    controls.update()
    solarMaterial.uniforms.time!.value = now * .001
    coronaMaterial.uniforms.time!.value = now * .001
    corona.quaternion.copy(camera.quaternion)
    sun.scale.setScalar(data.realisticScale ? .00465047 * WORLD_AU : 1.35)
    earthGroup.scale.setScalar(data.realisticScale ? (6371.2 / 149597870.7) * WORLD_AU : magneticSnapshot&&(!data.guided||data.field)?.07:.53)
    corona.scale.setScalar(data.realisticScale ? .041 : 1)
    grid.visible = data.grid&&activePreset!=='sun'
    fieldGroup.visible = data.field && !magneticSnapshot && dipoleMoment(data.time) !== null
    globalField.visible=data.field&&!!magneticSnapshot
    upstreamArrows.visible=!!magneticSnapshot&&!!data.field&&!!data.flow&&activePreset!=='sun'
    const speed=data.wind?.speed
    const flowVisible=!!data.flow
    flowLines.visible=flowVisible;windArrow.visible=flowVisible;windPaths.visible=flowVisible
    if(flowVisible){
      const minRadius=data.realisticScale?.00465047*WORLD_AU:1.42,maxRadius=activePreset==='sun'?5:16
      const age=data.wind?.observedAt===undefined?Infinity:Date.now()/1000-data.wind.observedAt
      const phase=data.time*(age>=-120&&age<=900&&typeof speed==='number'&&Number.isFinite(speed)&&speed>0?speed:0)/149597870.7*WORLD_AU
      flowDirections[flowDirections.length-1]!.copy(earthVector).normalize()
      const directions=data.guided?flowDirections.filter((_,i)=>i%6===0||i===flowDirections.length-1):flowDirections
      flowGeometry.setDrawRange(0,directions.length*6);windPathsGeometry.setDrawRange(0,directions.length*2)
      for(let i=0;i<directions.length;i++){
        const direction=directions[i]!,near=magneticSnapshot&&data.field?2:.8,along=direction.dot(earthVector),cross2=earthVector.lengthSq()-along*along
        const stop=along>0&&cross2<near*near?Math.min(maxRadius,along-Math.sqrt(near*near-cross2)):maxRadius
        const extent=Math.max(.01,stop-minRadius)
        const radius=minRadius+((i*.61803398875*extent+phase)%extent+extent)%extent
        const head=direction.clone().multiplyScalar(radius),tail=direction.clone().multiplyScalar(Math.max(minRadius,radius-.7))
        const side=new THREE.Vector3().crossVectors(direction,Math.abs(direction.y)>.9?new THREE.Vector3(1,0,0):new THREE.Vector3(0,1,0)).normalize().multiplyScalar(.09)
        const back=head.clone().addScaledVector(direction,-.22)
        flowPositions.set([...tail.toArray(),...head.toArray(),...back.clone().add(side).toArray(),...head.toArray(),...back.sub(side).toArray(),...head.toArray()],i*18)
        windPathsPositions.set([...direction.clone().multiplyScalar(minRadius).toArray(),...direction.clone().multiplyScalar(stop).toArray()],i*6)
      }
      flowGeometry.attributes.position!.needsUpdate=true
      windPathsGeometry.attributes.position!.needsUpdate=true
      const direction=earthVector.clone().normalize();windArrow.position.copy(direction.clone().multiplyScalar(minRadius+.2));windArrow.setDirection(direction)
    }
    const analysis=usableAnalysis(data.event)
    cmeArrow.visible=!!analysis
    if(analysis&&data.event){
      const at=Date.parse(analysis.time21_5||data.event.startTime)/1000
      if(Number.isFinite(at)){
        const direction=new THREE.Vector3(...heeqDirection(analysis.longitude!,analysis.latitude!,at,earthPosition(at))).applyAxisAngle(new THREE.Vector3(0,1,0),referenceAngle)
        cmeArrow.position.copy(direction.clone().multiplyScalar(sun.scale.x*1.1));cmeArrow.setDirection(direction)
      }else cmeArrow.visible=false
    }
    observedSun.visible=!!observation
    if(observation){
      const radiusFraction=observation.metadata.rsun/observation.metadata.width
      observedSun.scale.setScalar(sun.scale.x/Math.max(.1,radiusFraction))
      const observer=earthAt(Date.parse(observation.observed_at)/1000).normalize()
      const pole=new THREE.Vector3(...solarNorth(Date.parse(observation.observed_at)/1000)).applyAxisAngle(new THREE.Vector3(0,1,0),referenceAngle)
      observedSun.up.copy(pole);observedSun.position.copy(observer.clone().multiplyScalar(sun.scale.x*.015));observedSun.lookAt(observer.clone().multiplyScalar(10))
      const up=pole.clone().addScaledVector(observer,-pole.dot(observer)).normalize(),right=up.clone().cross(observer).normalize()
      const center=new THREE.Vector2((observation.metadata.refPixelX-.5)/observation.metadata.width,1-(observation.metadata.refPixelY-.5)/observation.metadata.height)
      observedMaterial.uniforms.radius!.value=radiusFraction;observedMaterial.uniforms.center!.value.copy(center)
      solarMaterial.uniforms.observerAxis!.value.copy(observer);solarMaterial.uniforms.observerUp!.value.copy(up);solarMaterial.uniforms.observerRight!.value.copy(right);solarMaterial.uniforms.observedRadius!.value=radiusFraction;solarMaterial.uniforms.observedCenter!.value.copy(center)
      sun.visible=true;corona.visible=false
    }else{sun.visible=true;corona.visible=true}
    const distance = null
    if (document.hidden || width < 2 || height < 2) return
    bloom.enabled = data.quality === 'high'
    if(data.trainingActive) renderer.render(scene,camera)
    else composer.render()
    count++
    if (now - lastReport > 450) {
      report({fps: Math.round(count * 1000 / (now - lastReport)), sun: project(new THREE.Vector3(0, data.realisticScale ? .07 : 1.65, 0)), earth: project(earthVector.clone().add(new THREE.Vector3(0, data.realisticScale ? .1 : Math.max(.18,earthGroup.scale.x*1.6), 0))), distance,
        orbits: [.25, .5, .75, 1].map(au => ({au, position: project(new THREE.Vector3(WORLD_AU * au, .02, .12))})),solarObserved:!!observation,fieldModel:magneticSnapshot?.model||'IGRF-14 degree 1',fieldLines:magneticSnapshot?.lines.length||48,windTracers:flowVisible?(data.guided?13:flowDirections.length):0,
        fieldSides:magneticSnapshot&&data.field?{sunward:project(earthVector.clone().addScaledVector(earthVector.clone().normalize(),-14*earthGroup.scale.x)),tailward:project(earthVector.clone().addScaledVector(earthVector.clone().normalize(),32*earthGroup.scale.x))}:undefined})
      count = 0; lastReport = now
    }
  }
  resize()
  frame = requestAnimationFrame(animate)
  function contextLost(event: Event) {event.preventDefault(); cancelAnimationFrame(frame); fail('WebGL 上下文已丢失。请重载三维视图；官方二维模型仍可使用。')}
  return {
    preset,
    dispose() {
      disposed = true; cancelAnimationFrame(frame); observer.disconnect(); controls.dispose()
      renderer.domElement.removeEventListener('webglcontextlost', contextLost)
      renderer.domElement.removeEventListener('pointermove', move)
      renderer.domElement.removeEventListener('pointerdown', down)
      renderer.domElement.removeEventListener('click', click)
      scene.traverse(object => {
        const item = object as THREE.Mesh
        item.geometry?.dispose()
        const materials = Array.isArray(item.material) ? item.material : item.material ? [item.material] : []
        materials.forEach(material => material.dispose())
      })
      textures.forEach(item => item.dispose())
      solarGeneration++;solarTexture?.dispose()
      composer.dispose(); bloom.dispose(); renderer.dispose(); renderer.forceContextLoss()
      renderer.domElement.remove()
    },
  }
}
