import {test} from 'node:test'
import assert from 'node:assert/strict'
import {guideContent} from './src/components/space/guide.ts'
const now=Date.parse('2026-11-12T10:00Z')/1000
const context={now,wind:{},modelStatus:{state:'idle'},event:null,simulations:[]}
test('position lesson explains the purpose without declaring a current storm',()=>{
 const c=guideContent(0,context)
 assert.match(c.description,/空间关系/)
 assert.match(c.limit,/没有据此判断当前有危险/)
})
test('wind lesson distinguishes fresh measured numbers from a direction illustration',()=>{
 const c=guideContent(1,{...context,wind:{observedAt:now-60,speed:400,density:5}})
 assert.match(c.state,/400 km\/s/)
 assert.match(c.limit,/多少根线不代表实际密度/)
 const expired=guideContent(1,{...context,wind:{observedAt:now-3600,speed:400,density:5}})
 assert.match(expired.state,/缺失、过期或不匹配/)
 assert.doesNotMatch(expired.state,/400/)
})
test('unavailable global model never gets described as a compressed magnetosphere',()=>{
 const c=guideContent(2,{...context,modelStatus:{state:'error',message:'缺少Dst'}})
 assert.match(c.description,/先看地球内源磁场/)
 assert.match(c.state,/未取得.*缺少Dst/)
 assert.doesNotMatch(c.description,/现在.*延伸成磁尾/)
})
test('manual field snapshot cannot be presented as observation or impact animation',()=>{
 const c=guideContent(2,{...context,magnetic:{driver_mode:'manual',parameters:{time:now}}})
 assert.match(c.state,/手动条件实验/)
 assert.match(c.limit,/模型快照.*不是某次喷发/)
})
test('failed model refresh labels retained snapshot instead of pretending a new result',()=>{
 const c=guideContent(2,{...context,magnetic:{driver_mode:'observed',parameters:{time:now-600}},modelStatus:{state:'error',message:'缺少Dst'}})
 assert.match(c.state,/更新失败，保留此快照/)
 assert.match(c.state,/缺少Dst/)
})
test('event lesson preserves unknown/no-Earth estimate and passed-time verification',()=>{
 const event={activityID:'CME-1',startTime:'2026-11-11T00:00Z'}
 const source={cmeInputs:[{CMEID:event.activityID}],impactList:[{location:'Mars',arrivalTime:'2026-11-13T00:00Z'}]}
 assert.match(guideContent(3,{...context,event,simulations:[source]}).state,/未给出地球到达/)
 assert.match(guideContent(3,{...context,event,simulations:[{...source,estimatedShockArrivalTime:'2026-11-12T09:00Z'}]}).state,/待观测|仍需.*核对/)
 assert.match(guideContent(3,context).state,/先选择/)
})
