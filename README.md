## Requirements

1. The user inputs their info and hits Start, then the bot will start.
2. Once the bot detects and hits Accept, it will send a Telegram text
3. It will continue watching the page for Accept and hit Accept everytime it sees it
4. The bot will continue doing this indefinitely until the timer runs out, at which point the bot will be killed and stop
5. If the user hits Start again while the bot is running, nothing should happen and the bot should keep running
6. If the user hits Start while the bot is dead, it should start again
7. If the user hits Stop while the bot is running, it should kill the bot, but the user should be able to hit Start again later and restart the bot
8. Only one instance of a bot should ever be running at one time
9. If the user hits Refresh, it should refresh the timer back to the max time
10. If the timer hits 0, it should kill the bot, but the user should still be able to start the bot again later
11. If the bot dies or is stopped, the timer should reset back to 0
12. The timer should automatically reset to the max duration whenever the bot is started
13. Before pressing the accept button, it should make sure it can extract the hospital, patient name, and patient id, and then send that in the telegram message. If any of these steps fail, do not hit Accept, and it should just keep waiting for another notification to show up and tries again.
14. The bot will hard kill itself if it is going 5 min longer than the timer duration in case the timer thread breaks.
15. If there is ever an error sending a telegram message, the bot should kill itself.
16. If the refresh button is pressed, the hard limit of when the bot kills itself if the timer thread dies resets.
17. The sevaro bot UI is accessible from any device on the tailnet
18. All chrome related commands, such as logging in and pressing the accept button, is done with a VPN so that it looks like they requests are coming from the VPN address
19. There is an "Acknowledge Accept" button in the UI.  Once the bot accepts a case, it will keep sending a telegram notification every 30 seconds, and block accepting any new cases until the user hits this "Acknowledge Accept" button.
20. The "Acknowledge Accept" will be grayed out normally, but will become active once the bot has accepted a new case.  Once the user hits this button it will gray out again.
21. If the failsafe goes off, it will kill the bot but the website is still accessible and the user can start the bot again in the future
22. If the bot fails to accept, it will notify the user and then continue looking for future accepts

## To build the package and publish to docker hub:

`docker buildx build   --platform linux/amd64,linux/arm64   -t kingish123/sevaro-runner:latest   --push .`

## To pull, build, and run on server:

`sudo docker compose rm -sf sevaro-bot`

`sudo docker pull kingish123/sevaro-runner:latest`

`sudo docker compose up --build -d sevaro-bot`

To do all at once:
`sudo docker compose rm -sf sevaro-bot && sudo docker pull kingish123/sevaro-runner:latest && sudo docker compose up --build -d sevaro-bot`