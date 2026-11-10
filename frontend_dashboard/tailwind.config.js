/** @type {import('tailwindcss').Config} */
// spec/03 §3 Design Tokens —— the single source of truth is src/theme/tokens.ts;
// this only provides same-name mappings for the cases that need CSS variables (bg-surface / text-primary, etc.),
// color values match tokens.ts verbatim; forbid hardcoding color literals elsewhere.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // background
        'bg-base': '#0B0E14',
        surface: '#121722',
        border: '#1A2230',
        // text
        primary: '#E6EDF3',
        secondary: '#8B98A9',
        accent: '#3B82F6',
        // status (0 offline / 1 normal / 2 warning / 3 fault)
        offline: '#5A6472',
        normal: '#2ECC71',
        warning: '#F1C40F',
        fault: '#FF4D4F',
      },
      fontSize: {
        'card-id': ['13px', { lineHeight: '18px', fontWeight: '600' }],
        'metric': ['11px', { lineHeight: '16px', fontWeight: '400' }],
        'panel-title': ['18px', { lineHeight: '24px', fontWeight: '700' }],
      },
      borderRadius: {
        card: '10px',
      },
    },
  },
  plugins: [],
};
