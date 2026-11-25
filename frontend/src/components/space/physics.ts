/** Auditable physical reference models, never a global space-weather simulation. */
import { Body, MakeTime, RotationAxis, Rotation_EQD_EQJ, Rotation_EQJ_ECL, RotateVector, SiderealTime, Vector } from 'astronomy-engine'

export type Triple = [number, number, number]
export interface WindSample { speed?: number; density?: number; bz?: number; observedAt?: number }
export type Topic = 'sun' | 'earth' | 'field' | 'wind' | 'cme' | 'shock'
export interface Inspection { topic: Topic; x: number; y: number; radiusRe?: number; magnitudeNt?: number }

/** HEEQ direction at analysis time: solar north Z, Earth-meridian equatorial X. */
export function heeqDirection(longitude: number, latitude: number, seconds: number, earth: Triple): Triple {
  if (!Number.isFinite(longitude) || Math.abs(longitude)>180 || !Number.isFinite(latitude) || Math.abs(latitude)>90 || !Number.isFinite(seconds) || !earth.every(Number.isFinite)) throw new Error('无效HEEQ方向参数')
  const z = solarNorth(seconds)
  const dot = earth.reduce((sum,v,i) => sum+v*z[i]!,0)
  const projection = earth.map((v,i) => v-dot*z[i]!)
  const norm = Math.hypot(...projection)
  if (!Number.isFinite(norm) || norm < 1e-9) throw new Error('HEEQ参考轴不可定义')
  const x = projection.map(v => v/norm)
  const y = [z[1]*x[2]!-z[2]*x[1]!,z[2]*x[0]!-z[0]*x[2]!,z[0]*x[1]!-z[1]*x[0]!]
  const lon=longitude*Math.PI/180,lat=latitude*Math.PI/180
  return x.map((v,i) => v*Math.cos(lat)*Math.cos(lon)+y[i]!*Math.cos(lat)*Math.sin(lon)+z[i]!*Math.sin(lat)) as Triple
}
const radians = Math.PI / 180

export function decimalYear(seconds: number) {
  const year = new Date(seconds * 1000).getUTCFullYear()
  return year + (seconds * 1000 - Date.UTC(year, 0, 1)) / (Date.UTC(year + 1, 0, 1) - Date.UTC(year, 0, 1))
}

/** IGRF-14 degree ONE only. nT, geographic Cartesian x=Greenwich, y=90E, z=north.
 * NOAA official 2025 coefficients and 2025–30 secular variation:
 * https://www.ngdc.noaa.gov/IAGA/vmod/coeffs/igrf14coeffs.txt
 * Higher harmonics, external currents, magnetopause and storms are NOT included.
 */
export function dipoleMoment(seconds: number): Triple | null {
  const year = decimalYear(seconds)
  if (!Number.isFinite(year) || year < 2025 || year > 2030) return null
  const dt = year - 2025
  return [-1410.3 + 10 * dt, 4545.5 - 21.5 * dt, -29350 + 12.6 * dt]
}

/** Field coordinates in reference Earth radii (6371.2 km), output nT. */
export function dipoleField(position: Triple, seconds: number): Triple | null {
  const moment = dipoleMoment(seconds)
  const r = Math.hypot(...position)
  if (!moment || !Number.isFinite(r) || r < 1 - 1e-9 || r > 3.001) return null
  const unit = position.map(v => v / r) as Triple
  const dot = moment.reduce((sum, v, i) => sum + v * unit[i]!, 0)
  return unit.map((v, i) => (3 * dot * v - moment[i]!) / r ** 3) as Triple
}

/** A solar plasma surface is not a rigid sphere: NASA latitude-dependent law. */
export function solarRotationDegreesPerDay(latitude: number) {
  if (!Number.isFinite(latitude) || Math.abs(latitude) > 90) throw new Error('无效太阳纬度')
  const s = Math.sin(latitude * radians) ** 2
  return 14.37 - 2.33 * s - 1.56 * s ** 2
}

function sceneVector(vector: Vector): Triple {
  const result = RotateVector(Rotation_EQJ_ECL(), vector)
  return [result.x, result.z, -result.y]
}

/** Earth geographic basis in the same J2000 ecliptic frame as the ephemeris.
 * Uses apparent Greenwich sidereal time, precession and nutation. No arbitrary
 * fixed 23-degree tilt or frame-rate-dependent spin is added.
 */
export function earthBasis(seconds: number): { x: Triple; north: Triple; z: Triple } {
  const date = new Date(seconds * 1000)
  const angle = SiderealTime(date) * Math.PI / 12
  const rotation = Rotation_EQD_EQJ(date)
  const convert = (x: number, y: number, z: number) => sceneVector(RotateVector(rotation, new Vector(x, y, z, MakeTime(date))))
  return {x: convert(Math.cos(angle), Math.sin(angle), 0), north: convert(0, 0, 1), z: convert(Math.sin(angle), -Math.cos(angle), 0)}
}

/** Solar pole orientation only; surface longitude is advected by differential rotation. */
export function solarNorth(seconds: number): Triple {
  return sceneVector(RotationAxis(Body.Sun, new Date(seconds * 1000)).north)
}

export function protonDynamicPressure(sample: WindSample): number | null {
  if (!(typeof sample.speed === 'number' && Number.isFinite(sample.speed) && sample.speed > 0 &&
    typeof sample.density === 'number' && Number.isFinite(sample.density) && sample.density > 0)) return null
  // m_p n v², cm^-3 -> m^-3 and km/s -> m/s, Pa -> nPa.
  return 1.67262192369e-6 * sample.density * sample.speed ** 2
}
