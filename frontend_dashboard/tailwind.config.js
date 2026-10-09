/** @type {import('tailwindcss').Config} */
// spec/03 §3 Design Tokens —— 单一事实来源是 src/theme/tokens.ts，
// 此处仅为需要走 CSS 变量的场景（bg-surface / text-primary 等）提供同名映射，
// 色值与 tokens.ts 逐字一致，禁止在别处写死颜色字面量。
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // 背景
        'bg-base': '#0B0E14',
        surface: '#121722',
        border: '#1A2230',
        // 文字
        primary: '#E6EDF3',
        secondary: '#8B98A9',
        accent: '#3B82F6',
        // 状态（0 离线 / 1 正常 / 2 预警 / 3 故障）
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
