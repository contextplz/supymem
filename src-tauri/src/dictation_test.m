// Deterministic lifecycle checks. These never request permissions or record.
#import "dictation.m"
#include <assert.h>

static int callbacks;
static NSString *finalText;
static BOOL cancelledBeforeStart;
static void receiveTest(void *context, int kind, const char *text, const char *error) {
    assert(context == (void *)1);
    assert(kind == 2);
    assert(error == NULL);
    assert(activeDictation != nil); // The session remains owned through its callback.
    callbacks++;
    finalText = [NSString stringWithUTF8String:text];
}

static void receiveCanceled(void *context, int kind, const char *text, const char *error) {
    assert(context == (void *)2);
    assert(kind == 2 && error == NULL);
    assert(text[0] == '\0' && activeDictation == nil);
    cancelledBeforeStart = YES;
    callbacks++;
}

static SupymemDictation *session(NSString *identifier) {
    SupymemDictation *value = [[SupymemDictation alloc] init];
    value.sessionID = identifier;
    value.context = (void *)1;
    value.callback = receiveTest;
    activeDictation = value;
    return value;
}

int main(void) {
    @autoreleasepool {
        // Stop while permission is pending: terminal once, no microphone start.
        SupymemDictation *pending = session(@"pending");
        [pending stop];
        [pending stop];
        [pending authorizeMicrophone]; // Late permission callback is harmless.
        assert(callbacks == 1 && pending.finished && !pending.started);
        assert(activeDictation == nil && pending.context == NULL);

        // Stop after capture: leave time to flush before the terminal event.
        SupymemDictation *listening = session(@"listening");
        listening.started = YES;
        listening.transcript = @"an edited idea";
        [listening stop];
        assert(callbacks == 1 && listening.stopping && !listening.finished);
        [listening finish:nil];
        [listening finish:nil];
        assert(callbacks == 2 && [finalText isEqualToString:@"an edited idea"]);
        assert(activeDictation == nil);

        // Finishing an unrelated session cannot clear the active one.
        SupymemDictation *current = session(@"current");
        supymem_dictation_stop("older");
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.02]];
        assert(!current.stopping && !current.finished);
        SupymemDictation *older = [[SupymemDictation alloc] init];
        older.context = (void *)1;
        older.callback = receiveTest;
        [older finish:nil];
        assert(activeDictation == current);
        supymem_dictation_shutdown();
        assert(callbacks == 4 && activeDictation == nil);

        // Shutdown is also safe when the active pointer is the last owner.
        @autoreleasepool { session(@"last-owner"); }
        supymem_dictation_shutdown();
        assert(callbacks == 5 && activeDictation == nil);

        // A Stop can reach the bridge before an async Tauri Start is dispatched.
        // Unsupported locale keeps the before-fix check permission-free too.
        supymem_dictation_stop("cancel-before-start");
        supymem_dictation_start("cancel-before-start", "xx-NO-LANGUAGE", (void *)2, receiveCanceled);
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:2];
        while (!cancelledBeforeStart && deadline.timeIntervalSinceNow > 0) {
            [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
        }
        assert(cancelledBeforeStart && callbacks == 6 && activeDictation == nil);

        for (int i = 0; i < 100; i++) rememberCancellation([NSString stringWithFormat:@"pending-%d", i]);
        assert(cancelledStarts.count == 64);
        assert(consumeCancellation(@"pending-99") && !consumeCancellation(@"pending-99"));
    }
    return 0;
}
