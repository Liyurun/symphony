/** @type {import('tailwindcss').Config} */
export default {
  // 扫描这些文件里的类名，未使用的样式会被摇树移除
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  // 使用 class 策略切换深色主题（在 <html> 或 <body> 上加 dark 类）
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // 品牌强调色：indigo 系
        brand: {
          50: '#eef2ff',
          100: '#e0e7ff',
          200: '#c7d2fe',
          300: '#a5b4fc',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          800: '#3730a3',
          900: '#312e81',
        },
        // 深色背景基调：slate 系
        surface: {
          DEFAULT: '#0f172a', // slate-900
          light: '#1e293b',   // slate-800
          border: '#334155',  // slate-700
        },
        // Catppuccin Mocha 深色调色板（Dashboard 主界面使用）
        ctp: {
          base: '#181825',    // 页面背景
          mantle: '#1e1e2e',  // 面板背景
          surface: '#313244', // 卡片 / 边框
          text: '#cdd6f4',    // 主文字
          subtext: '#a6adc8', // 次要文字
          overlay: '#6c7086', // 弱化文字
          mauve: '#cba6f7',   // 强调紫
          blue: '#89b4fa',    // 运行中
          green: '#a6e3a1',   // 完成
          yellow: '#f9e2af',  // 等待介入
          red: '#f38ba8',     // 失败
          peach: '#fab387',   // 橙
        },
      },
    },
  },
  plugins: [],
}
