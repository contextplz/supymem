// Apple owns recognition and microphone capture; audio never leaves this Mac.
#import <AVFoundation/AVFoundation.h>
#import <Speech/Speech.h>

// 0 = microphone started, 1 = partial transcript, 2 = terminal transcript.
typedef void (*DictationCallback)(void *, int, const char *, const char *);

@interface SupymemDictation : NSObject
@property(nonatomic, copy) NSString *sessionID;
@property(nonatomic, copy) NSString *transcript;
@property(nonatomic) DictationCallback callback;
@property(nonatomic) void *context;
@property(nonatomic, strong) SFSpeechRecognizer *recognizer;
@property(nonatomic, strong) SFSpeechAudioBufferRecognitionRequest *request;
@property(nonatomic, strong) SFSpeechRecognitionTask *task;
@property(nonatomic, strong) AVAudioEngine *engine;
@property(nonatomic) BOOL tapped;
@property(nonatomic) BOOL started;
@property(nonatomic) BOOL stopping;
@property(nonatomic) BOOL finished;
- (void)finish:(NSString *)error;
- (void)authorizeMicrophone;
- (void)begin;
- (void)stop;
@end

// Only the main queue accesses the active session or session state.
static SupymemDictation *activeDictation;
static NSMutableArray<NSString *> *cancelledStarts;

static void rememberCancellation(NSString *identifier) {
    if (!cancelledStarts) cancelledStarts = [[NSMutableArray alloc] init];
    if (![cancelledStarts containsObject:identifier]) [cancelledStarts addObject:identifier];
    // ponytail: retain the latest 64 cancelled starts, enough for UI dispatch
    // races without keeping session history or transcript data.
    if (cancelledStarts.count > 64) [cancelledStarts removeObjectAtIndex:0];
}

static BOOL consumeCancellation(NSString *identifier) {
    if (![cancelledStarts containsObject:identifier]) return NO;
    [cancelledStarts removeObject:identifier];
    return YES;
}

static SFSpeechRecognizer *recognizerForLocale(NSString *locale) {
    NSLocale *language = locale.length ? [[NSLocale alloc] initWithLocaleIdentifier:locale] : NSLocale.currentLocale;
    return [[SFSpeechRecognizer alloc] initWithLocale:language];
}

@implementation SupymemDictation
- (void)stopMicrophone {
    [self.engine stop];
    if (self.tapped) {
        [self.engine.inputNode removeTapOnBus:0];
        self.tapped = NO;
    }
}

- (void)finish:(NSString *)error {
    if (self.finished) return;
    self.finished = YES;
    [self stopMicrophone];
    [self.request endAudio];
    [self.task cancel];
    self.task = nil; // Break the task/result-handler/session retain cycle.
    self.request = nil;
    self.engine = nil;
    self.callback(self.context, 2, (self.transcript ?: @"").UTF8String, error.UTF8String);
    self.context = NULL; // Rust releases the context on the terminal callback.
    // The active session can be its last owner during shutdown. Release it
    // only after the callback and the final access to its native context.
    if (activeDictation == self) activeDictation = nil;
}

- (void)authorizeMicrophone {
    if (self.finished) return;
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
    if (status == AVAuthorizationStatusAuthorized) {
        [self begin];
    } else if (status == AVAuthorizationStatusNotDetermined) {
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL granted) {
            dispatch_async(dispatch_get_main_queue(), ^{
                if (self.finished) return;
                if (granted) [self begin];
                else [self finish:@"Allow Microphone for Supymem in System Settings, then try Dictate again. You can also type your message."];
            });
        }];
    } else {
        [self finish:@"Allow Microphone for Supymem in System Settings, then try Dictate again. You can also type your message."];
    }
}

- (void)begin {
    if (self.finished) return;
    @try {
        // Check again after authorization. No network fallback is permitted.
        if (!self.recognizer.supportsOnDeviceRecognition || !self.recognizer.available) {
            [self finish:@"On-device dictation is unavailable for this language. Type your message and try Dictate again later."];
            return;
        }
        self.request = [[SFSpeechAudioBufferRecognitionRequest alloc] init];
        self.request.requiresOnDeviceRecognition = YES;
        self.request.shouldReportPartialResults = YES;
        self.request.taskHint = SFSpeechRecognitionTaskHintDictation;
        self.engine = [[AVAudioEngine alloc] init];
        AVAudioInputNode *input = self.engine.inputNode;
        AVAudioFormat *format = [input outputFormatForBus:0];
        if (format.sampleRate == 0 || format.channelCount == 0) {
            [self finish:@"No microphone is available. Connect one and try Dictate again, or type your message."];
            return;
        }
        SFSpeechAudioBufferRecognitionRequest *request = self.request;
        [input installTapOnBus:0 bufferSize:1024 format:format block:^(AVAudioPCMBuffer *buffer, __unused AVAudioTime *when) {
            [request appendAudioPCMBuffer:buffer];
        }];
        self.tapped = YES;
        self.recognizer.queue = NSOperationQueue.mainQueue;
        self.task = [self.recognizer recognitionTaskWithRequest:request resultHandler:^(SFSpeechRecognitionResult *result, NSError *error) {
            if (self.finished) return;
            if (result) self.transcript = result.bestTranscription.formattedString;
            if (result.isFinal || error) {
                [self finish:error && !self.stopping ? @"Dictation stopped. Try Dictate again, or type your message." : nil];
            } else if (result) {
                self.callback(self.context, 1, self.transcript.UTF8String, NULL);
            }
        }];
        [self.engine prepare];
        NSError *error = nil;
        if (![self.engine startAndReturnError:&error]) {
            [self finish:@"The microphone could not start. Check it and try Dictate again, or type your message."];
            return;
        }
        self.started = YES;
        self.callback(self.context, 0, "", NULL);
        // Apple's streaming recognizer has a short-session limit. End cleanly
        // with the latest transcript instead of leaving microphone capture on.
        __weak SupymemDictation *weakSession = self;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 55 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{ [weakSession stop]; });
    } @catch (NSException *exception) {
        [self finish:@"Dictation could not start. Check your microphone and try again, or type your message."];
    }
}

- (void)stop {
    if (self.finished || self.stopping) return;
    self.stopping = YES;
    [self stopMicrophone];
    if (!self.started) {
        [self finish:nil]; // Also cancels an in-flight permission request.
        return;
    }
    [self.request endAudio];
    [self.task finish]; // Flush buffered speech before emitting the final event.
    // A missing final callback must not leave the UI or native context stuck.
    __weak SupymemDictation *weakSession = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{ [weakSession finish:nil]; });
}
@end

bool supymem_dictation_available(const char *locale) {
    @autoreleasepool {
        // This probe does not ask for speech or microphone permissions.
        return recognizerForLocale([NSString stringWithUTF8String:locale]).supportsOnDeviceRecognition;
    }
}

void supymem_dictation_start(const char *sessionID, const char *locale, void *context, DictationCallback callback) {
    @autoreleasepool {
        NSString *identifier = [NSString stringWithUTF8String:sessionID];
        NSString *language = [NSString stringWithUTF8String:locale];
        dispatch_async(dispatch_get_main_queue(), ^{
            SupymemDictation *session = [[SupymemDictation alloc] init];
            session.sessionID = identifier;
            session.callback = callback;
            session.context = context;
            if (consumeCancellation(identifier)) {
                [session finish:nil];
                return;
            }
            if (activeDictation) {
                [session finish:@"Dictation is already listening. Stop it before starting again."];
                return;
            }
            activeDictation = session;
            session.recognizer = recognizerForLocale(language);
            if (!session.recognizer.supportsOnDeviceRecognition) {
                [session finish:@"On-device dictation is unavailable for this language. Type your message instead."];
                return;
            }
            SFSpeechRecognizerAuthorizationStatus status = SFSpeechRecognizer.authorizationStatus;
            if (status == SFSpeechRecognizerAuthorizationStatusAuthorized) {
                [session authorizeMicrophone];
            } else if (status == SFSpeechRecognizerAuthorizationStatusNotDetermined) {
                [SFSpeechRecognizer requestAuthorization:^(SFSpeechRecognizerAuthorizationStatus answer) {
                    dispatch_async(dispatch_get_main_queue(), ^{
                        if (session.finished) return;
                        if (answer == SFSpeechRecognizerAuthorizationStatusAuthorized) [session authorizeMicrophone];
                        else [session finish:@"Allow Speech Recognition for Supymem in System Settings, then try Dictate again. You can also type your message."];
                    });
                }];
            } else {
                [session finish:@"Allow Speech Recognition for Supymem in System Settings, then try Dictate again. You can also type your message."];
            }
        });
    }
}

void supymem_dictation_stop(const char *sessionID) {
    @autoreleasepool {
        NSString *identifier = [NSString stringWithUTF8String:sessionID];
        dispatch_async(dispatch_get_main_queue(), ^{
            // An unmount/stop from an older composer cannot stop a newer recording.
            if ([activeDictation.sessionID isEqualToString:identifier]) [activeDictation stop];
            else rememberCancellation(identifier);
        });
    }
}

void supymem_dictation_shutdown(void) {
    [activeDictation finish:nil];
}
