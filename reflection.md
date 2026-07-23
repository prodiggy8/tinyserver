Two things stood out for me in this homework. The first one is how powerful loops are. 
I build a similar HTTP web server from scratch in C++ as part of 15-441 (Networking) and,
since I was not using any generative AI, it took me about 50h over 3 weeks to get a similar
amount of features running. With a raw agent, I would have probably taken a day or so. With
loop engineering, I did it in just a few hours and with surprising quality. Another takeaway 
from this homework is how important it is to have well defined scopes for the agents. Tests for
Step 1 were written by the same loop that dealt with the implementation, which led to bugs encoded
in tests as correct behavior, which were only caught by the review step agent (Content length was 0
all the time due to stripping body before computation).
