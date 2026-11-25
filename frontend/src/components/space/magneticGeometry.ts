import * as THREE from 'three'
import type { MagneticModel } from './products'

/** GSM -> right-handed Three local convention (x, z, -y). All lengths remain Re. */
export const gsmPoint=(p:number[])=>new THREE.Vector3(p[0],p[2],-p[1]!)
export function clearGroup(group:THREE.Group){
  group.traverse(item=>{const drawable=item as THREE.Mesh;drawable.geometry?.dispose();const materials=Array.isArray(drawable.material)?drawable.material:drawable.material?[drawable.material]:[];materials.forEach(m=>m.dispose())})
  group.clear()
}
export function magneticGeometry(group:THREE.Group,model:MagneticModel){
  clearGroup(group)
  for(const path of model.lines){
    const positions=path.points.map(gsmPoint),colors:number[]=[]
    for(const point of path.points){const strength=point[3]||1;const color=new THREE.Color().setHSL(.58-Math.min(1,Math.max(0,Math.log10(Math.max(strength,3)/3)/4))*.38,.72,.58);colors.push(color.r,color.g,color.b)}
    const geometry=new THREE.BufferGeometry().setFromPoints(positions)
    geometry.setAttribute('color',new THREE.Float32BufferAttribute(colors,3))
    geometry.setAttribute('strength',new THREE.Float32BufferAttribute(path.points.map(p=>p[3]||0),1))
    const curve=new THREE.Line(geometry,new THREE.LineBasicMaterial({vertexColors:true,transparent:true,opacity:.72}))
    curve.userData.topic='field';group.add(curve)
  }
  // The independent Shue envelope is a labelled estimate, not a T96 isosurface.
  const {r0,alpha}=model.boundary
  const vertices:number[]=[],indices:number[]=[]
  const along=90,around=64
  let maxTheta=2.7
  for(let theta=.01;theta<2.7;theta+=.005){const r=r0*(2/(1+Math.cos(theta)))**alpha;if(r*Math.cos(theta)<-50){maxTheta=theta;break}}
  for(let i=0;i<=along;i++){
    const theta=i/along*maxTheta,r=r0*(2/(1+Math.cos(theta)))**alpha
    const x=r*Math.cos(theta),rho=r*Math.sin(theta)
    for(let j=0;j<=around;j++){
      const phi=j/around*Math.PI*2
      vertices.push(x,rho*Math.cos(phi),rho*Math.sin(phi))
    }
  }
  for(let i=0;i<along;i++)for(let j=0;j<around;j++){
    const a=i*(around+1)+j,b=a+around+1;indices.push(a,b,a+1,b,b+1,a+1)
  }
  const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(vertices,3));geometry.setIndex(indices);geometry.computeVertexNormals()
  const boundary=new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({color:0x4b8599,transparent:true,opacity:.018,depthWrite:false,side:THREE.DoubleSide}))
  boundary.userData.topic='field';boundary.userData.boundary=true;group.add(boundary)
  // Meridional guides reveal the whole estimated envelope without a solid shell.
  for(let j=0;j<8;j++){
    const points=[];const phi=j/8*Math.PI*2
    for(let i=0;i<=along;i++){const theta=i/along*maxTheta,r=r0*(2/(1+Math.cos(theta)))**alpha;points.push(new THREE.Vector3(r*Math.cos(theta),r*Math.sin(theta)*Math.cos(phi),r*Math.sin(theta)*Math.sin(phi)))}
    const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),new THREE.LineBasicMaterial({color:0x4897b0,transparent:true,opacity:.16}));group.add(line)
  }
}
