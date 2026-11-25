export const sphereVertex = `
varying vec2 vUv;
varying vec3 vNormal;
varying vec3 vWorld;
void main() {
  vUv = uv;
  vNormal = normalize(mat3(modelMatrix) * normal);
  vec4 world = modelMatrix * vec4(position, 1.0);
  vWorld = world.xyz;
  gl_Position = projectionMatrix * viewMatrix * world;
}`

export const noiseFunctions = `
float hash(vec3 p) { p = fract(p*.3183099+vec3(.1,.2,.3)); p *= 17.; return fract(p.x*p.y*p.z*(p.x+p.y+p.z)); }
float noise(vec3 p) {
  vec3 i=floor(p), f=fract(p); f=f*f*(3.-2.*f);
  return mix(mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x),mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
             mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);
}
float fbm(vec3 p) { float value=0.; float a=.5; for(int i=0;i<5;i++){value+=a*noise(p);p=p*2.04+1.7;a*=.5;}return value; }
`

export const sunFragment = `
uniform sampler2D surface;
uniform float time;
uniform float rotationDays;
uniform sampler2D observedMap;
uniform float hasObservation;
uniform float observedRadius;
uniform vec2 observedCenter;
uniform vec3 observerAxis;
uniform vec3 observerUp;
uniform vec3 observerRight;
varying vec2 vUv;
varying vec3 vNormal;
varying vec3 vWorld;
${noiseFunctions}
void main(){
  if(hasObservation>.5){
    vec3 n=normalize(vWorld);
    if(dot(n,observerAxis)>0.){
      vec2 sampleUv=observedCenter+observedRadius*vec2(dot(n,observerRight),dot(n,observerUp));
      gl_FragColor=vec4(texture2D(observedMap,sampleUv).rgb,1.);
    }else{
      // Explicit unknown hemisphere: no duplicated activity or generated far-side image.
      float stripe=step(.95,fract(vUv.x*24.))+step(.96,fract(vUv.y*18.));
      // Visible neutral grid identifies missing observations rather than making
      // the unobserved half disappear against the black background.
      gl_FragColor=vec4(vec3(.10,.14,.18)+stripe*vec3(.04,.07,.08),1.);
    }
    return;
  }
  vec3 p = normalize(vWorld)*18.;
  float grain = fbm(p+vec3(0,time*.012,0));
  float sine=sin((vUv.y-.5)*3.14159265359);
  float s2=sine*sine;
  float rate=14.37-2.33*s2-1.56*s2*s2;
  vec3 photo=texture2D(surface,vec2(fract(vUv.x-mod(rate*rotationDays,360.)/360.),vUv.y)).rgb;
  float granules=noise(p*18.);
  float limb=pow(max(dot(normalize(cameraPosition-vWorld),vNormal),0.),.28);
  vec3 plasma=mix(vec3(.65,.07,.005),vec3(2.1,.83,.18),grain);
  vec3 color=mix(plasma,photo*2.0,.65)*(0.87+granules*.22)*(.48+.52*limb);
  gl_FragColor=vec4(color,1.);
}`

/** Off-limb emission stays in the observation plane; its depth is not reconstructed. */
export const solarLimbFragment=`
uniform sampler2D surface;
uniform float radius;
uniform vec2 center;
varying vec2 vUv;
void main(){
  if(distance(vUv,center)<radius*.998)discard;
  vec3 color=texture2D(surface,vUv).rgb;
  gl_FragColor=vec4(color,1.);
}`

export const earthFragment = `
uniform sampler2D dayMap;
uniform sampler2D nightMap;
uniform sampler2D specMap;
varying vec2 vUv;
varying vec3 vNormal;
varying vec3 vWorld;
void main(){
  vec3 n=normalize(vNormal);
  vec3 light=normalize(-vWorld);
  vec3 viewDir=normalize(cameraPosition-vWorld);
  float sun=dot(n,light);
  float day=smoothstep(-.1,.18,sun);
  vec3 albedo=texture2D(dayMap,vUv).rgb;
  vec3 night=texture2D(nightMap,vUv).rgb;
  float spec=pow(max(dot(n,normalize(light+viewDir)),0.),50.)*texture2D(specMap,vUv).r*max(sun,0.);
  vec3 color=albedo*(.035+max(sun,0.)*1.3)*day+night*(1.-day)*1.15+vec3(.9,.82,.62)*spec*.65;
  float rim=pow(1.-max(dot(n,viewDir),0.),3.);
  color+=vec3(.06,.28,.65)*rim*smoothstep(-.2,.5,sun)*.55;
  gl_FragColor=vec4(color,1.);
}`

export const atmosphereFragment = `
varying vec3 vNormal;
varying vec3 vWorld;
void main(){
 float edge=pow(1.-abs(dot(normalize(vNormal),normalize(cameraPosition-vWorld))),3.2);
 float light=smoothstep(-.28,.65,dot(normalize(vNormal),normalize(-vWorld)));
 gl_FragColor=vec4(mix(vec3(.015,.06,.2),vec3(.18,.5,1.),light),edge*.55);
}`

// Optical display halo only: it does not encode a coronal magnetic field or flow.
export const coronaFragment = `
varying vec2 vUv;
void main(){
 vec2 p=vUv*2.-1.;float r=length(p);
 float halo=exp(-r*7.5)*.4;
 gl_FragColor=vec4(vec3(1.0,.33,.045),halo);
}`
