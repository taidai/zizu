import { useId } from 'react'

export type EquipmentKind = 'solar' | 'battery' | 'charger' | 'factory' | 'cabinet' | 'grid'

// Pure artwork from the approved tablet Demo; no demonstration state or data.
export default function EquipmentIcon({ kind, size = 80 }: { kind: EquipmentKind; size?: number }) {
  const id = useId().replace(/:/g, '')
  const silver = `url(#${id}s)`
  const gold = `url(#${id}g)`
  return <svg width={size} height={size} viewBox="0 0 100 100" data-equipment={kind} aria-hidden="true">
    <defs>
      <linearGradient id={`${id}s`} x1="0" y1="0" x2="1" y2=".8"><stop stopColor="#fff" /><stop offset=".38" stopColor="#c7ccd0" /><stop offset=".56" stopColor="#fafbfc" /><stop offset="1" stopColor="#777f85" /></linearGradient>
      <linearGradient id={`${id}g`} x1="0" x2="1"><stop stopColor="#ffe197" /><stop offset=".45" stopColor="#b17b1e" /><stop offset=".8" stopColor="#f6d57a" /><stop offset="1" stopColor="#8e5e15" /></linearGradient>
    </defs>
    <ellipse cx="50" cy="88" rx="34" ry="5" fill="#6c747c" opacity=".14" />
    {kind === 'solar' ? <g stroke="#596570" strokeWidth="1.6"><path d="M43 64v20h31M47 64l9 20" fill="none" /><path d="M19 29h68L73 72H5z" fill={silver} /><path d="M23 33h58L70 66H12z" fill="#75889a" />{[0, 1, 2, 3].map(n => <path key={n} d={`M${34 + n * 12} 33l-11 33M${20 - n * 3} ${40 + n * 7}h58`} stroke="#dbe4eb" strokeWidth="1" />)}<circle cx="74" cy="15" r="8" fill={gold} stroke="#8e5e15" /></g>
      : kind === 'battery' ? <g stroke="#687078" strokeWidth="1.5"><rect x="28" y="14" width="44" height="68" rx="6" fill={silver} /><rect x="40" y="8" width="20" height="6" rx="2" fill={silver} /><rect x="34" y="24" width="32" height="49" rx="2" fill="#e3e7e8" /><path d="M34 43h32v30H34z" fill={gold} /><path d="M34 53h32M34 63h32" stroke="#fff4d0" /><path d="M32 19h34" stroke="white" /></g>
      : kind === 'charger' ? <g stroke="#667079" strokeWidth="1.5"><path d="M31 13l30-3 6 6v67H30z" fill={silver} /><path d="M61 11v70" /><rect x="36" y="22" width="18" height="24" rx="2" fill="#3e4b55" /><rect x="40" y="26" width="10" height="8" fill="#d5ad50" /><path d="M50 52l-9 13h7l-3 10 12-16h-8z" fill={gold} /><path d="M65 32h10q10 0 9 12l-1 25q0 12-10 7l-5-7" fill="none" strokeWidth="3" /><path d="M24 84h47v4H24z" fill={silver} /></g>
      : kind === 'factory' ? <g stroke="#616b73" strokeWidth="1.5"><path d="M16 80V42l20 12V36l23 14V25h13v55z" fill={silver} /><path d="M69 15h10v65H69zM59 51l20 9M29 81V65h10v16" fill={silver} />{[0, 1, 2].map(i => <path key={i} d={`M${44 + i * 11} 60h5v7h-5zM${44 + i * 11} 72h5v6h-5z`} fill="#627482" />)}</g>
      : kind === 'cabinet' ? <g stroke="#677078" strokeWidth="1.4"><path d="M18 18l50-6 17 10v61l-50 6-17-8z" fill={silver} /><path d="M35 25l50-3M35 25v63M18 18l17 7M60 24v61" fill="none" />{[0, 1, 2, 3].map(n => <g key={n}><rect x="40" y={32 + n * 12} width="15" height="8" fill="#58616b" /><rect x="43" y={34 + n * 12} width="5" height="4" fill="#d5ad50" /><path d={`M66 ${34 + n * 12}h12`} stroke="#8a939a" /></g>)}</g>
      : <g stroke="#6c757e" strokeWidth="2" fill="none"><path d="M44 8h12L80 87M44 8L20 87M24 32h52M16 55h68M38 29l27 26M62 29L35 55M34 57l40 30M66 57L26 87M10 32h80M5 55h90" /><path d="M46 9L25 84M54 9l21 75" stroke="#d4d9df" strokeWidth="1.2" /></g>}
  </svg>
}
