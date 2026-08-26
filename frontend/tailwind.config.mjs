/** @type {import('tailwindcss').Config} */
export default {
  content: ['./app/**/*.{js,ts,jsx,tsx}', './components/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f0f4ff',
          100: '#dce6ff',
          200: '#b8ccff',
          300: '#85a8ff',
          400: '#507aff',
          500: '#2550f5',
          600: '#1a3edb',
          700: '#1530b1',
          800: '#172b8f',
          900: '#192971'
        }
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Menlo', 'monospace']
      }
    }
  },
  plugins: []
}
