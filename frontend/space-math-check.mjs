import { test } from 'node:test'
import assert from 'node:assert/strict'
import { earthPosition, frontDistanceAu, usableAnalysis, eventDirection, AU_KM } from './src/components/space/sceneMath.ts'

const event={activityID:'test',startTime:'2026-11-11T00:00:00Z',cmeAnalyses:[{speed:500,longitude:0,latitude:0,halfAngle:35,isMostAccurate:true}]}
test('Earth ephemeris distance is expressed in AU',()=>{
  for(const date of ['2026-01-01','2026-07-01','2026-11-11']){
    const p=earthPosition(Date.parse(date)/1000)
    assert.ok(Math.hypot(...p)>.98&&Math.hypot(...p)<1.02)
  }
})
test('no invented propagation for missing direction, width, or speed',()=>{
  assert.equal(usableAnalysis(null),null)
  assert.equal(usableAnalysis({...event,cmeAnalyses:[{speed:500,longitude:null,latitude:0,halfAngle:35}]}),null)
  assert.equal(usableAnalysis({...event,cmeAnalyses:[{speed:0,longitude:0,latitude:0,halfAngle:35}]}),null)
  assert.equal(usableAnalysis({...event,cmeAnalyses:[{speed:500,longitude:0,latitude:0,halfAngle:150}]}),null)
})
test('constant-speed visual radius has correct units and no pre-event shell',()=>{
  const start=Date.parse(event.startTime)/1000
  assert.equal(frontDistanceAu(event,start-3600),0)
  assert.ok(Math.abs(frontDistanceAu(event,start+3600)-500*3600/AU_KM)<1e-12)
  assert.equal(frontDistanceAu({...event,startTime:'unknown'},start),null)
})
test('Earth-relative direction preserves known zero longitude rather than treating it missing',()=>{
  const direction=eventDirection(event.cmeAnalyses[0],[1,0,0])
  assert.deepEqual(direction,[1,0,0])
})
