import {test} from 'node:test'
import assert from 'node:assert/strict'
import {associatedModels,earthImpact,upstreamIntake} from './src/components/space/impact.ts'
const event={activityID:'CME-A',startTime:'2026-11-10T00:00Z'}
test('other-body arrival never becomes Earth arrival, absent Earth does not prove no effects',()=>{
 const s={simulationID:'WSA-ENLIL/1/1',modelCompletionTime:'2026-11-10T01:00Z',cmeInputs:[{CMEID:'CME-A'}],impactList:[{location:'Mars',arrivalTime:'2026-11-12T00:00Z'}]}
 assert.equal(earthImpact(event,[s],0).arrival,null)
 assert.equal(earthImpact(event,[s],0).state,'no_earth_estimate')
 assert.equal(earthImpact(event,[s],0).probability,null)
})
test('embedded explicit association is accepted, revisions prefer latest even if it removes estimate',()=>{
 const e={...event,cmeAnalyses:[{enlilList:[{link:'https://ccmc.gsfc.nasa.gov/DONKI/view/WSA-ENLIL/1/-1',modelCompletionTime:'2026-11-10T01:00Z',estimatedShockArrivalTime:'2026-11-12T00:00Z'}]}]}
 assert.equal(associatedModels(e,[]).length,1)
 const newer={simulationID:'WSA-ENLIL/1/2',cmeInputs:[{CMEID:'CME-A'}],modelCompletionTime:'2026-11-10T02:00Z',estimatedShockArrivalTime:null}
 assert.equal(earthImpact(e,[newer],0).arrival,null)
})
test('passed estimate awaits observation; same date is not an association',()=>{
 const s={cmeInputs:[{CMEID:'CME-A'}],estimatedShockArrivalTime:'2026-11-12T00:00Z'}
 assert.equal(earthImpact(event,[s],Date.parse('2026-11-13')/1000).state,'awaiting_confirmation')
 assert.equal(earthImpact(event,[{...s,cmeInputs:[{CMEID:'OTHER'}]}],0).state,'unknown')
})
test('density and flux units, stale data and unknown are separate',()=>{
 assert.equal(upstreamIntake({speed:400,density:5,observedAt:1000},1000).fluxPerSquareMetreSecond,2e12)
 assert.equal(upstreamIntake({speed:400,density:5,observedAt:0},2000).fluxPerSquareMetreSecond,null)
 assert.equal(upstreamIntake({speed:400,observedAt:1000},1000).valid,false)
})
