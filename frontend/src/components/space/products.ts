export interface MagneticModel {
  model: string; kind: string; coordinate: string
  parameters: {time:number;pressure:number;dst:number;by:number;bz:number}
  basis_geo: [number,number,number][]
  lines: {points:number[][];ends:string[]}[]
  boundary: {model:string;r0:number;alpha:number}
  driver_mode: string; driver_note:string; warnings:string[]; limits:string
}
export interface SolarImage {
  id:string;image_id:string;channel:string;observed_at:string;requested_at:string;offset_seconds:number;source_url:string;note:string
  selection?:'latest'|'at_time';cache_hit?:boolean
  metadata:{width:number;height:number;rsun:number;refPixelX:number;refPixelY:number;rotation:number}
}
export interface ModelFrame {id:string;run:string;valid_at:string;parameter:string}
