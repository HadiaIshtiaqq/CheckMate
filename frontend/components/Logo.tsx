// CHECKMATE mark: a chess king's crown resting on a checkmark — "check" + "mate",
// i.e. a purchase that has been verified and settled. Burgundy tile, white glyph.
export function LogoMark({ size = 40, className = "" }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" className={className} role="img" aria-label="CHECKMATE logo">
      <defs>
        <linearGradient id="cm-tile" x1="6" y1="4" x2="44" y2="46" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#de97a9" />
          <stop offset="0.55" stopColor="#a8506a" />
          <stop offset="1" stopColor="#76374a" />
        </linearGradient>
        <linearGradient id="cm-shine" x1="24" y1="2" x2="24" y2="26" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.35" />
          <stop offset="1" stopColor="#ffffff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="44" height="44" rx="13" fill="url(#cm-tile)" />
      <rect x="2" y="2" width="44" height="22" rx="13" fill="url(#cm-shine)" />
      {/* crown */}
      <path d="M14.5 21.5 13 12.5l5.6 4.3L24 9.5l5.4 7.3 5.6-4.3-1.5 9z" fill="#fff" />
      <circle cx="13" cy="11.6" r="1.7" fill="#fff" />
      <circle cx="24" cy="8.4" r="1.9" fill="#fff" />
      <circle cx="35" cy="11.6" r="1.7" fill="#fff" />
      <rect x="14.5" y="22.6" width="19" height="2.6" rx="1.3" fill="#fff" opacity="0.9" />
      {/* check */}
      <path d="M15 31.5l6 5.8L33.5 27" fill="none" stroke="#fff" strokeWidth="4.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`font-extrabold tracking-tight ${className}`}>
      <span className="text-brand-900 dark:text-white">Check</span>
      <span className="bg-gradient-to-r from-brand-500 to-brand-700 bg-clip-text text-transparent">mate</span>
    </span>
  );
}
