Create a CronJob named log-cleaner in namespace scenario-test that runs every 6 hours. The job should use a busybox:1.36 image and execute the command: find /tmp -mtime +1 -delete
