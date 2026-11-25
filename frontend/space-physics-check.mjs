import {test} from 'node:test'
import assert from 'node:assert/strict'
import {dipoleField,dipoleMoment,earthBasis,solarNorth,solarRotationDegreesPerDay,protonDynamicPressure,heeqDirection} from './src/components/space/physics.ts'
import {earthPosition} from './src/components/space/sceneMath.ts'
const at=Date.parse('2025-01-01T00:00Z')/1000
const dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0)
test('official IGRF14 2025 degree-one coefficients and validity window',()=>{
 assert.deepEqual(dipoleMoment(at),[-1410.3,4545.5,-29350])
 assert.deepEqual(dipoleField([0,0,1],at),[1410.3,-4545.5,-58700])
 assert.equal(dipoleMoment(Date.parse('2024-12-31')/1000),null)
 assert.equal(dipoleMoment(Date.parse('2031-01-01')/1000),null)
 assert.equal(dipoleField([0,0,.9],at),null)
 assert.equal(dipoleField([0,0,4],at),null)
})
test('dipole strength decays as r^-3 without an invented magnetopause',()=>{
 const a=Math.hypot(...dipoleField([1.2,0,0],at))
 const b=Math.hypot(...dipoleField([2.4,0,0],at))
 assert.ok(Math.abs(a/b-8)<1e-10)
})
test('reference field satisfies numerical divergence-free vacuum constraint',()=>{
 const point=[1.8,.2,.4],h=1e-5
 let divergence=0
 for(let axis=0;axis<3;axis++){
  const a=[...point],b=[...point];a[axis]+=h;b[axis]-=h
  divergence+=(dipoleField(a,at)[axis]-dipoleField(b,at)[axis])/(2*h)
 }
 assert.ok(Math.abs(divergence)<.0001)
})
test('terrestrial pose is orthonormal and right-handed and advances with time',()=>{
 const basis=earthBasis(at)
 for(const v of [basis.x,basis.north,basis.z])assert.ok(Math.abs(Math.hypot(...v)-1)<1e-10)
 assert.ok(Math.abs(dot(basis.x,basis.north))<1e-10)
 const cross=[basis.x[1]*basis.north[2]-basis.x[2]*basis.north[1],basis.x[2]*basis.north[0]-basis.x[0]*basis.north[2],basis.x[0]*basis.north[1]-basis.x[1]*basis.north[0]]
 assert.ok(Math.hypot(...cross.map((v,i)=>v-basis.z[i]))<1e-10)
 assert.ok(Math.abs(dot(basis.x,earthBasis(at+21600).x))<.01)
})
test('Sun uses latitude-dependent physical reference rotation and solar pole',()=>{
 assert.equal(solarRotationDegreesPerDay(0),14.37)
 assert.ok(Math.abs(solarRotationDegreesPerDay(90)-10.48)<1e-12)
 assert.ok(solarRotationDegreesPerDay(0)>solarRotationDegreesPerDay(70))
 const tilt=Math.acos(solarNorth(at)[1])*180/Math.PI
 assert.ok(tilt>7&&tilt<7.5)
})
test('proton-only dynamic pressure uses correct dimensions and rejects missing data',()=>{
 assert.ok(Math.abs(protonDynamicPressure({speed:400,density:5})-1.338097538952)<1e-10)
 assert.equal(protonDynamicPressure({speed:400}),null)
 assert.equal(protonDynamicPressure({speed:NaN,density:5}),null)
})
test('HEEQ uses the solar equator and pole, not an ecliptic longitude approximation',()=>{
 const earth=earthPosition(at),north=solarNorth(at)
 const x=heeqDirection(0,0,at,earth),y=heeqDirection(90,0,at,earth),z=heeqDirection(0,90,at,earth)
 for(const v of [x,y,z])assert.ok(Math.abs(Math.hypot(...v)-1)<1e-10)
 assert.ok(Math.abs(dot(x,north))<1e-10)
 assert.ok(Math.abs(dot(x,y))<1e-10)
 assert.ok(Math.hypot(...z.map((v,i)=>v-north[i]))<1e-10)
 assert.ok(dot(x,earth)>0)
 const cross=[x[1]*y[2]-x[2]*y[1],x[2]*y[0]-x[0]*y[2],x[0]*y[1]-x[1]*y[0]]
 assert.ok(Math.hypot(...cross.map((v,i)=>v-z[i]))<1e-10)
 assert.throws(()=>heeqDirection(NaN,0,at,earth))
})
