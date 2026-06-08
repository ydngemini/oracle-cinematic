'use client'

import { useCallback, useEffect, useState } from 'react'
import type { Waypoint, CameraPose, Vec2 } from './tourData'
import * as staticData from './tourData'

export interface TourSchema {
  property: { name: string; tagline: string; style: string }
  footprint: { width: number; depth: number }
  waypoints: Waypoint[]
  tour_order: string[]
  explore_pose: CameraPose
  dollhouse_pose: CameraPose
}

type Status = 'idle' | 'loading' | 'ready' | 'error'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

function apiSchemaToTourSchema(data: any): TourSchema {
  return {
    property: data.property,
    footprint: data.footprint,
    waypoints: data.waypoints.map((wp: any) => ({
      id: wp.id,
      name: wp.name,
      tag: wp.tag,
      pose: { position: wp.pose.position as [number, number, number], target: wp.pose.target as [number, number, number] },
      hotspot: wp.hotspot as [number, number, number],
      narration: wp.narration,
      plan: { center: wp.plan.center as [number, number], size: wp.plan.size as [number, number] },
      stats: wp.stats,
    })),
    tour_order: data.tour_order,
    explore_pose: data.explore_pose,
    dollhouse_pose: data.dollhouse_pose,
  }
}

function staticToSchema(): TourSchema {
  return {
    property: { name: 'Luxury Penthouse', tagline: 'Skyline living redefined', style: 'luxury-penthouse' },
    footprint: staticData.FOOTPRINT,
    waypoints: staticData.WAYPOINTS,
    tour_order: staticData.TOUR_ORDER,
    explore_pose: staticData.EXPLORE_POSE,
    dollhouse_pose: staticData.DOLLHOUSE_POSE,
  }
}

export function useTourData(propertyInput?: Record<string, unknown>) {
  const [schema, setSchema] = useState<TourSchema>(staticToSchema)
  const [status, setStatus] = useState<Status>('ready')
  const [error, setError] = useState<string | null>(null)

  const generate = useCallback(async (data: Record<string, unknown>) => {
    setStatus('loading')
    setError(null)
    try {
      const res = await fetch(`${API_URL}/api/generate-tour`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: res.statusText }))
        throw new Error(err.error || `HTTP ${res.status}`)
      }
      const json = await res.json()
      setSchema(apiSchemaToTourSchema(json))
      setStatus('ready')
    } catch (e: any) {
      setError(e.message || 'Generation failed')
      setStatus('error')
    }
  }, [])

  useEffect(() => {
    if (propertyInput && Object.keys(propertyInput).length > 0) {
      generate(propertyInput)
    }
  }, []) // only on mount

  const waypointIndex: Record<string, Waypoint> = Object.fromEntries(
    schema.waypoints.map(w => [w.id, w])
  )

  return { schema, status, error, generate, waypointIndex }
}
