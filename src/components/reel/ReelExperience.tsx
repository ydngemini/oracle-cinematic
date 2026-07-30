import { projects } from '@/data/projects'
import { ReelSequence } from './ReelSequence'
import styles from './reel.module.css'

export function ReelExperience() {
  return (
    <div className={styles.reel}>
      <a className={styles.skipLink} href="#reel-main">
        Skip to project sequence
      </a>
      <ReelSequence projects={projects} />
    </div>
  )
}
