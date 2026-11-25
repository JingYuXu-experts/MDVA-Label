import { Body, HelioVector, RotateVector, Rotation_EQJ_ECL } from 'astronomy-engine'

export const AU_KM = 149_597_870.7
export const WORLD_AU = 12
export interface CmeAnalysis {
  speed?: number | null
  longitude?: number | null
  latitude?: number | null
  halfAngle?: number | null
  isMostAccurate?: boolean
  time21_5?: string | null
}
export interface SceneEvent {
  activityID: string
  startTime: string
  cmeAnalyses?: CmeAnalysis[] | null
}

/** J2000 ecliptic coordinates in AU, independent of the display camera. */
export function earthPosition(seconds: number): [number, number, number] {
  const vector = RotateVector(Rotation_EQJ_ECL(), HelioVector(Body.Earth, new Date(seconds * 1000)))
  return [vector.x, vector.z, -vector.y]
}

export function usableAnalysis(event: SceneEvent | null): CmeAnalysis | null {
  const analyses = event?.cmeAnalyses || []
  const valid = analyses.filter(a =>
    typeof a.speed === 'number' && Number.isFinite(a.speed) && a.speed > 0 &&
    typeof a.longitude === 'number' && Number.isFinite(a.longitude) && Math.abs(a.longitude) <= 180 &&
    typeof a.latitude === 'number' && Number.isFinite(a.latitude) && Math.abs(a.latitude) <= 90 &&
    typeof a.halfAngle === 'number' && Number.isFinite(a.halfAngle) && a.halfAngle > 0 && a.halfAngle <= 90)
  return valid.find(a => a.isMostAccurate) || valid[0] || null
}

/** Constant-speed parameter visualization, never a shock-arrival prediction. */
export function frontDistanceAu(event: SceneEvent | null, seconds: number): number | null {
  const analysis = usableAnalysis(event)
  const start = event ? Date.parse(event.startTime) / 1000 : NaN
  if (!analysis || !Number.isFinite(start) || !Number.isFinite(seconds)) return null
  return Math.max(0, seconds - start) * analysis.speed! / AU_KM
}

export function eventDirection(analysis: CmeAnalysis, earth: [number, number, number]): [number, number, number] {
  // DONKI Earth-relative angular parameters are projected into a simplified
  // Sun–Earth frame. This is explicitly not a reconstruction of an ENLIL volume.
  const earthAngle = Math.atan2(earth[2], earth[0])
  const longitude = analysis.longitude! * Math.PI / 180
  const latitude = analysis.latitude! * Math.PI / 180
  return [Math.cos(earthAngle + longitude) * Math.cos(latitude), Math.sin(latitude), Math.sin(earthAngle + longitude) * Math.cos(latitude)]
}
