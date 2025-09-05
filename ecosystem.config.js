// Path: ~/projects/documind-hr/ecosystem.config.js

module.exports = {
  apps: [
    {
      name: "documind-backend",
      cwd: "/home/subhash.thakur.india/projects/documind-hr",
      script: "venv/bin/python",
      args: "-m uvicorn main:app --host 0.0.0.0 --port 9000 --workers 2",
      env: {
        DOCUMIND_HR_DSN: "postgresql://postgres:postgres@127.0.0.1:5432/documind_hr",
        FILES_DIR: "/home/subhash.thakur.india/projects/documind-hr/data",
        PORT: "9000"
      },
      autorestart: true,
      restart_delay: 3000
    },
    {
      name: "documind-ui",
      cwd: "/home/subhash.thakur.india/projects/documind-hr/ui",
      script: "bash",
      args: "-lc 'npx vite --host 0.0.0.0 --port 5173'",
      // If you’re on NVM Node 22, pin it so PM2 always uses the same Node:
      // interpreter: "/home/subhash.thakur.india/.nvm/versions/node/v22.18.0/bin/node",
      env: { HOST: "0.0.0.0", PORT: "5173" },
      autorestart: true,
      restart_delay: 3000
    }
  ]
}
