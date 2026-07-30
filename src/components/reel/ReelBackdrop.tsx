import Image from 'next/image'
import styles from './reel.module.css'

export function ReelBackdrop() {
  return (
    <div
      className={styles.homeBackdrop}
      data-reel-backdrop
      aria-hidden="true"
    >
      <Image
        src="/projects/fall-line-house/hero.webp"
        alt=""
        fill
        sizes="100vw"
        quality={72}
      />
      <span />
    </div>
  )
}
