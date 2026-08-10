# BotByte — Free SQLite + Backup System

Adds:
- Admin Panel → Create Local Backup
- Admin Panel → Backup to GitHub
- Admin Panel → List/Restore GitHub backups
- Automatic GitHub backup every 6 hours when configured

Use a PRIVATE GitHub repository. Put the fine-grained GitHub token only in Render Environment Variables.

Recommended before every upgrade:
1. Create Local Backup.
2. Backup to GitHub.
3. Deploy.
4. Restore if needed.

A backup cannot recover data that was already lost and has no existing backup.
